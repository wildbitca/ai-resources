"""OpenClaw host operations: units, the drained doctor, bootstrap, status, agent workspaces.

One implementation, two entry points. The setup wizard's OpenClaw section
(`setup/cockpits/_openclaw_host.py`) calls these functions; `ai-resources openclaw <verb>`
is a thin wrapper over the same functions for headless runs and disaster recovery. No logic
lives only in a subcommand.

Two rules run through the whole module, both learned the hard way on a live gateway:

* Nothing here writes ~/.openclaw/openclaw.json. OpenClaw journals and fingerprints that file;
  every change goes through `openclaw config patch --stdin` (see `cockpits/openclaw.py`).
* Nothing here restarts the gateway on its own initiative. `doctor` stops and starts it, but
  only because that is what its user asked for, and it is built so the gateway is always
  started again (T01). Every other verb reports "restart needed" and stops. The one sanctioned
  self-initiated restart is `graceful_restart` (ADR-0004): bounded by gates, only on confirmed
  memory pressure, never `--force`, with watchdog.off held by a signal-safe marker guard.

Every function that touches the system takes an injectable `runner`, so the tests drive the
real state machines against a fake and nothing in the suite can reach a live unit.
"""
from __future__ import annotations

import argparse
import ipaddress
import copy
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, NamedTuple

from . import model_pins, openclaw_pressure, openclaw_reload_rules, repo_root

# (return code, combined output). 127 means the binary is missing.
Runner = Callable[..., "tuple[int, str]"]

BREW_OPENCLAW_FALLBACKS = ("/home/linuxbrew/.linuxbrew/bin/openclaw", "/opt/homebrew/bin/openclaw",
                           "/usr/local/bin/openclaw")
HOST_ENV_PATH = Path.home() / ".openclaw" / "kit-host.env"
WATCHDOG_OFF = Path.home() / ".openclaw" / "watchdog.off"
GATEWAY_UNIT = "openclaw-gateway.service"


def _run_capped(argv: list[str], *, env: dict | None, timeout: float | None, cwd: str | None,
                max_bytes: int) -> tuple[int, str]:
    """Run `argv` and keep at most `max_bytes` of its output: the process is killed once the cap is reached, so a
    huge output is never held in memory. The (successful) exit code is reported as 0 in that case."""
    import threading
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env, cwd=cwd or str(Path.home()))
    except FileNotFoundError:
        return 127, f"{argv[0]} not found"
    except OSError as e:
        return 1, str(e)
    buf = bytearray()
    capped = threading.Event()

    def reader() -> None:
        assert proc.stdout is not None
        while len(buf) < max_bytes:
            chunk = proc.stdout.read(min(65536, max_bytes - len(buf)))
            if not chunk:
                return
            buf.extend(chunk)
        capped.set()
        proc.kill()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        proc.kill()
        t.join(5)
        proc.wait()
        return 124, f"timed out after {timeout:g}s"
    rc = proc.wait()
    return (0 if capped.is_set() else rc), bytes(buf).decode("utf-8", errors="replace").strip()


def default_runner(argv: list[str], *, env: dict | None = None, timeout: float | None = 120,
                   input: str | None = None, cwd: str | None = None,
                   max_bytes: int | None = None) -> tuple[int, str]:
    """Run `argv`; (return code, combined output).

    127 is a missing binary and 124 a timeout (the coreutils `timeout` convention); any other
    OSError is 1. Every caller before the 124 split tested `rc == 0` only, so it is additive.
    `max_bytes` keeps only that much output and stops the process there (for a read of untrusted text).
    """
    if max_bytes is not None and input is None:
        return _run_capped(argv, env=env, timeout=timeout, cwd=cwd, max_bytes=max_bytes)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=input,
                           env=env, cwd=cwd or str(Path.home()))
    except FileNotFoundError:
        return 127, f"{argv[0]} not found"
    except subprocess.TimeoutExpired as e:
        return 124, str(e)
    except OSError as e:
        return 1, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


# --- resource-safety knobs (ADR-0004) -----------------------------------------------------------------------
#
# The same keys the health-restart script reads from ~/.openclaw/kit-host.env (defaults in
# scripts/openclaw/_common.sh). The process environment wins over the file, the file over the default.

RESTART_KNOB_DEFAULTS: dict[str, str] = {
    "OPENCLAW_GRACEFUL_RESTART": "notify",       # off | notify | on
    "OPENCLAW_RESTART_WINDOW": "02:00-05:00",    # HH:MM-HH:MM in OPENCLAW_RESTART_TZ; "" = never inside a window
    "OPENCLAW_RESTART_TZ": "America/Guayaquil",  # the host clock is UTC; the operator's zone is explicit
    "OPENCLAW_RESTART_HARD_PCT": "105",          # of MemoryHigh; overrides the window
    "OPENCLAW_RESTART_PRESSURE_PCT": "90",
    "OPENCLAW_RESTART_COOLDOWN_S": "10800",
    "OPENCLAW_RESTART_DAILY_CAP": "2",
    "OPENCLAW_HEALTH_CONFIRM_S": "60",
    "OPENCLAW_RESTART_SNAPSHOTS_KEEP": "10",
    "OPENCLAW_FROZEN_MIN_S": "600",
    "OPENCLAW_RESTART_SETTLE_S": "120",
}


def restart_knobs(env: dict[str, str] | None = None, host_env: dict[str, str] | None = None) -> dict[str, str]:
    """Effective knob values as strings (process env, then the host env file, then the default)."""
    env = os.environ if env is None else env
    file_env = read_host_env() if host_env is None else host_env
    out: dict[str, str] = {}
    for key, default in RESTART_KNOB_DEFAULTS.items():
        val = env.get(key)
        if val is None:
            val = file_env.get(key)
        out[key] = default if val is None else val.strip()
    return out


def knob_number(knobs: dict[str, str], key: str) -> float:
    """A numeric knob; a malformed value falls back to the default (never an exception in a timer)."""
    try:
        return float(knobs[key])
    except (KeyError, ValueError):
        return float(RESTART_KNOB_DEFAULTS[key])


def kit_root() -> Path:
    """The version-independent kit root: what a unit or hook may safely point at."""
    from .setup.cockpits import _shared
    return _shared.stable_kit_root(repo_root())


def resolve_openclaw_bin() -> str:
    """Absolute path of `openclaw`, resolved at render time so a unit never depends on a login PATH."""
    found = shutil.which("openclaw")
    if found:
        return found
    for candidate in BREW_OPENCLAW_FALLBACKS:
        if os.path.exists(candidate):
            return candidate
    return "openclaw"


# --- host env file ----------------------------------------------------------------------------

def read_host_env(path: Path | None = None) -> dict[str, str]:
    """Parse ~/.openclaw/kit-host.env (plain KEY=value lines; comments and blanks ignored)."""
    path = path or HOST_ENV_PATH
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


def host_env_problem(path: Path | None = None) -> str:
    """Why `write_host_env(path)` could not write, as one clause, or "" when it can. Checked BEFORE any command
    that creates something (2.2c, 6b): a directory or a device behind `kit-host.env` must not be found after the
    gcloud configuration was already created."""
    import stat as _stat
    path = path or HOST_ENV_PATH
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    suffix = f" (a link to {target})" if path.is_symlink() else ""
    try:
        mode = os.stat(target).st_mode
    except FileNotFoundError:
        if path.is_symlink() and not target.parent.is_dir():
            return f"{path} is a symlink to {target}, whose directory does not exist"
        parent = target.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if not os.access(parent, os.W_OK | os.X_OK):
            return f"{parent} is not writable, so {path} cannot be created"
        return ""
    except OSError as e:
        return f"{path} cannot be inspected ({e.strerror or type(e).__name__})"
    if _stat.S_ISDIR(mode):
        return f"{path}{suffix} is a directory, not a file"
    if not _stat.S_ISREG(mode):
        return f"{path}{suffix} is not a regular file"
    if not os.access(target.parent, os.W_OK | os.X_OK):
        return f"{target.parent} is not writable, so {path} cannot be replaced"
    return ""


_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*\Z")   # \Z, never `$`: `$` also matches before a trailing newline
# The keys the kit writes into kit-host.env: the host values of `_configure_env` and the two off-box knobs. Teardown
# writes a recorded previous value back only under one of these: a hand-edited state must not be able to make it
# write `LD_PRELOAD=` or `BASH_ENV=` (the file is sourced by bash), nor a knob the operator owns such as
# `OPENCLAW_OFFBOX_LIST_CMD` (a command line).
KIT_ENV_KEYS = frozenset({
    "OPENCLAW_OWNER_TELEGRAM_ID", "OPENCLAW_BACKUP_DIR", "OPENCLAW_PUBLIC_DOMAIN", "OPENCLAW_POD_CIDR",
    "OPENCLAW_NARRATION", "OPENCLAW_GUARD", "OPENCLAW_GRACEFUL_RESTART", "OPENCLAW_RESOURCE_GUARDS",
    "OPENCLAW_OFFBOX_GCLOUD_CONFIG", "OPENCLAW_OFFBOX_IMPERSONATE_SA"})
# No space: the file is sourced by bash, so `KEY=a /path/cmd` would run /path/cmd.
_ENV_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_@%+=:,./~-]*\Z")


def _check_env_pair(key: str, value: str) -> None:
    if not _ENV_KEY.match(key):
        raise ValueError(f"bad host env key: {key!r}")
    if not _ENV_SAFE_VALUE.match(value):
        # The file is sourced by bash: refuse anything that could be code.
        raise ValueError(f"unsafe characters in the value of {key}: {value!r}")


def write_file_atomically(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write `text` to a unique temp file beside `path` and move it over `path`.

    The old inode is never written to, so another hard link to it keeps the bytes it had, and a planted
    `.<name>.tmp` is never followed. `mode` defaults to the mode `path` has now, else 0644."""
    if mode is None:
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_host_env(values: dict[str, str | None], path: Path | None = None) -> bool:
    """Set (or, for None, remove) the given keys, keeping every other line as it is.

    The file is created with the shipped example's header on first write. Returns True when it
    changed, so a second run with the same answers is a byte-level no-op.
    """
    path = path or HOST_ENV_PATH
    for key, value in values.items():
        if value is not None:
            _check_env_pair(key, value)
    if path.is_symlink():
        # A dotfile manager's link: the target is written and the link survives (the same rule as a managed
        # block). A link whose target directory does not exist is left alone, with one line.
        target = Path(os.path.realpath(path))
        if not target.parent.is_dir():
            print(f"{path} is a symlink to {target}, whose directory does not exist; left alone")
            return False
        path = target
    try:
        original = path.read_text(encoding="utf-8")
    except OSError:
        original = "# Managed by `ai-resources setup`. Plain KEY=value lines, sourced by bash.\n"
    lines = original.splitlines()
    pending = dict(values)
    out: list[str] = []
    for line in lines:
        key = line.partition("=")[0].strip().lstrip("#").strip()
        if "=" in line and key in pending:
            value = pending.pop(key)
            if value is not None:
                out.append(f"{key}={value}")
            continue
        out.append(line)
    for key, value in pending.items():
        if value is not None:
            out.append(f"{key}={value}")
    new = "\n".join(out).rstrip("\n") + "\n"
    if new == original:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp file (created 0600 and exclusively, so a planted `kit-host.env.tmp` is never followed and two
    # writers never share a name), given the mode the file already has, then moved over it.
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    from .setup import state as setup_state
    setup_state.remove_stale_temp_files(path)      # a SIGKILLed earlier write leaves `.kit-host.env.*.tmp`
    return True


# --- systemd units -----------------------------------------------------------------------------

TEMPLATES_DIR_NAME = Path("templates") / "systemd"
UNIT_NAMES = (
    "openclaw-backup@.service",
    "openclaw-backup-daily.timer",
    "openclaw-backup-weekly.timer",
    "openclaw-backup-monthly.timer",
    "openclaw-backup-guard.service",
    "openclaw-backup-guard.timer",
    "openclaw-backup-uploader.service",
    "openclaw-backup-uploader.timer",
    "openclaw-maintenance.service",
    "openclaw-maintenance.timer",
    "openclaw-watchdog.service",
    "openclaw-watchdog.timer",
    "openclaw-verify.service",
    "openclaw-verify.timer",
    "openclaw-models-update.service",
    "openclaw-models-update.timer",
    "openclaw-health-restart.service",
    "openclaw-health-restart.timer",
)
TIMER_NAMES = tuple(n for n in UNIT_NAMES if n.endswith(".timer"))
_MARKER = re.compile(r"@([A-Z][A-Z0-9_]*)@")


def user_unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def unit_markers(libexec: Path | str | None = None) -> dict[str, str]:
    libexec = str(libexec or kit_root())
    if re.search(r"\s", libexec):
        # ExecStart is whitespace-separated: a path with a space would run the wrong program.
        raise ValueError(f"the kit path contains whitespace and cannot go into a unit: {libexec!r}")
    return {"LIBEXEC": libexec}


def render_unit(name: str, markers: dict[str, str], templates_dir: Path | None = None) -> str:
    """One unit from its template. A marker without a value raises: a literal `@X@` in a unit
    would install fine and fail at the first firing."""
    templates_dir = templates_dir or (repo_root() / TEMPLATES_DIR_NAME)
    text = (templates_dir / f"{name}.template").read_text(encoding="utf-8")
    missing = sorted({m for m in _MARKER.findall(text) if m not in markers})
    if missing:
        raise KeyError(f"{name}: no value for marker(s) {', '.join(missing)}")
    return _MARKER.sub(lambda m: markers[m.group(1)], text)


def render_units(markers: dict[str, str] | None = None,
                 templates_dir: Path | None = None) -> dict[str, str]:
    markers = markers or unit_markers()
    return {name: render_unit(name, markers, templates_dir) for name in UNIT_NAMES}


# --- GitOps backup templates (C08) -------------------------------------------------------------------

GITOPS_TEMPLATES_DIR_NAME = Path("templates") / "gitops" / "openclaw-backups"
GITOPS_TEMPLATE_NAMES = ("bucket", "uploader", "guard", "alerts")
_GITOPS_VALUE = re.compile(r"^[A-Za-z0-9._:/@+-]+$")


def gitops_marker_names(templates_dir: Path | None = None) -> list[str]:
    """Every marker the four backup templates use, sorted. The values belong to the target
    infrastructure (project, bucket, node, WIF pool ...) and live in its own repo, never here."""
    templates_dir = templates_dir or (repo_root() / GITOPS_TEMPLATES_DIR_NAME)
    found: set[str] = set()
    for name in GITOPS_TEMPLATE_NAMES:
        found |= set(_MARKER.findall((templates_dir / f"{name}.yaml.template").read_text(encoding="utf-8")))
    return sorted(found)


def render_gitops_backups(markers: dict[str, str], templates_dir: Path | None = None) -> dict[str, str]:
    """The four GitOps manifests as {"bucket.yaml": text, ...}.

    A marker without a value raises, and so does a value that could change the YAML's structure
    (whitespace, quotes, a newline): a half-substituted manifest applies fine and fails in the
    cluster, at the worst time.
    """
    templates_dir = templates_dir or (repo_root() / GITOPS_TEMPLATES_DIR_NAME)
    missing = sorted(set(gitops_marker_names(templates_dir)) - set(markers))
    if missing:
        raise KeyError("no value for marker(s) " + ", ".join(missing))
    for key, value in markers.items():
        if not _GITOPS_VALUE.match(str(value)):
            raise ValueError(f"{key}: {value!r} has characters that are not safe in a manifest")
    return {f"{name}.yaml": _MARKER.sub(lambda m: str(markers[m.group(1)]),
                                       (templates_dir / f"{name}.yaml.template").read_text(encoding="utf-8"))
            for name in GITOPS_TEMPLATE_NAMES}


HEALTH_UNITS = ("openclaw-health-restart.service", "openclaw-health-restart.timer")
HAND_SCRIPT_NAME = "openclaw-health-restart.sh"


def hand_copy_files(dest: Path | None = None, home: Path | None = None,
                    rendered: dict[str, str] | None = None) -> list[Path]:
    """The hand-installed health-restart script and unit files that are not the kit's render.

    The host kept its own copy of this timer before the kit adopted it (ADR-0003). A unit file that
    equals the kit's render is not a hand copy; the script under ~/.local/bin always is, because
    the kit's units run the script from the kit root.
    """
    dest = dest or user_unit_dir()
    home = home or Path.home()
    found: list[Path] = []
    script = home / ".local" / "bin" / HAND_SCRIPT_NAME
    if script.is_file() or script.is_symlink():           # a dangling link is still the operator's file
        found.append(script)
    rendered = rendered if rendered is not None else render_units()
    for name in HEALTH_UNITS:
        target = dest / name
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            if target.is_symlink():                      # dangling: moved aside as a link, never written over
                found.append(target)
            continue
        if text != rendered.get(name):
            found.append(target)
    return found


def _adopt_hand_copy(files: list[Path], *, runner: Runner, home: Path, now: str | None = None) -> list[str]:
    """Stop the hand-installed timer (not the gateway), then MOVE its files aside; never delete them."""
    runner(["systemctl", "--user", "disable", "--now", "openclaw-health-restart.timer"], env=systemd_env())
    backup = home / ".openclaw" / "backup" / "hand-units" / (now or time.strftime("%Y%m%d-%H%M%S"))
    backup.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for f in files:
        target = backup / f.name
        shutil.move(str(f), str(target))
        moved.append(str(target))
    return moved


def install_units(dest: Path | None = None, *, markers: dict[str, str] | None = None,
                  dry_run: bool = False, enable: bool = False, runner: Runner = default_runner,
                  templates_dir: Path | None = None, adopt_hand_copy: bool = False,
                  home: Path | None = None) -> dict:
    """Write all the units at once, so the host never runs a mix of old and new ones.

    Returns {"changed": [...], "unchanged": [...], "reloaded": bool, "enabled": [...],
             "hand_copy": [paths], "adopted": [paths]}.
    Never touches the gateway unit. A second run with nothing to change writes nothing and
    reloads nothing. A hand-installed health-restart copy is left alone (its units are not
    written) unless `adopt_hand_copy`: then its timer is disabled and its files are moved to
    ~/.openclaw/backup/hand-units/<timestamp>/ before the kit's units go in.
    """
    dest = dest or user_unit_dir()
    home = home or Path.home()
    rendered = render_units(markers, templates_dir)
    hand = hand_copy_files(dest, home, rendered)
    skip: set[str] = set()
    if hand and not adopt_hand_copy:
        skip = set(HEALTH_UNITS)
    changed, unchanged = [], []
    for name, text in rendered.items():
        if name in skip:
            continue
        target = dest / name
        try:
            same = target.read_text(encoding="utf-8") == text
        except OSError:
            same = False
        (unchanged if same else changed).append(name)
    result = {"changed": changed, "unchanged": unchanged, "reloaded": False, "enabled": [],
              "hand_copy": [str(p) for p in hand], "adopted": []}
    if dry_run:
        return result
    if hand and adopt_hand_copy:
        result["adopted"] = _adopt_hand_copy(hand, runner=runner, home=home)
    if changed:
        dest.mkdir(parents=True, exist_ok=True)
        for name in changed:
            write_file_atomically(dest / name, rendered[name])
        rc, _out = runner(["systemctl", "--user", "daemon-reload"], env=systemd_env())
        result["reloaded"] = rc == 0
    if enable:
        for timer in TIMER_NAMES:
            if timer in skip:
                continue          # a hand copy owns this timer until the operator adopts it
            rc, _out = runner(["systemctl", "--user", "enable", "--now", timer], env=systemd_env())
            if rc == 0:
                result["enabled"].append(timer)
    return result


def systemd_env() -> dict[str, str]:
    """`systemctl --user` fails from a timer or a bare SSH shell without these two."""
    env = dict(os.environ)
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env["XDG_RUNTIME_DIR"] = runtime
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    return env


# --- the drained doctor (T01) ---------------------------------------------------------------------

EXIT_OK = 0
EXIT_REFUSED = 2          # another maintenance window is already open
EXIT_NOT_DRAINED = 3      # the cgroup never emptied, so `doctor --fix` was not run
EXIT_DOCTOR_FAILED = 4    # `doctor --fix` ran and did not complete
EXIT_NOT_HEALTHY = 5      # the gateway did not answer after being started again


KIT_MARKER_WORD = "kit-run"           # the first word of a marker a kit run wrote (an operator's is empty)
_KIT_MARKER = re.compile(r"kit-run pid=(\d+) start=(\d+)(?: |$)")


def process_start_ticks(pid: int) -> int | None:
    """The start time of process `pid` (field 22 of /proc/<pid>/stat, clock ticks since boot), None when there
    is no such process or no procfs. Together with the pid it names ONE process: a pid that was reused has
    another start time."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return int(text.rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _kit_run_alive(pid: int, start: int) -> bool:
    ticks = process_start_ticks(pid)
    if ticks is not None:
        return start == 0 or ticks == start
    if Path("/proc/self/stat").exists():
        return False                       # procfs is there and has no such process
    try:
        os.kill(pid, 0)                    # no procfs: the best that can be asked
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _hold_termination() -> set[int] | None:
    """Block SIGTERM and SIGINT for this thread; the mask to restore, None when it cannot be done (not the main
    thread, no `pthread_sigmask`): then the caller runs unprotected as before."""
    import signal
    import threading
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "pthread_sigmask"):
        return None
    try:
        return signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    except (ValueError, OSError):
        return None


def _release_termination(previous: set[int] | None) -> None:
    import signal
    if previous is not None:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


class Marker:
    """The `watchdog.off` file: while it exists the watchdog leaves the gateway alone.

    A marker a kit run creates with `acquire()` carries a signature line (`kit-run pid=<pid> start=<ticks>
    at=<epoch>`): who made it, so a leftover of a run that was killed is told from a window an operator opened
    (an empty file, or any other content). Only the operator's marker is a maintenance window; a signed marker
    whose process is gone is stale (`stale_kit_marker`, `reclaim_stale`) and the models job does not stand down
    for it (kit 2.2e, 4c)."""

    def __init__(self, path: Path | None = None):
        self.path = path or WATCHDOG_OFF
        # (device, inode, mtime) of the file this object created: an inode number is reused at once when a file
        # is deleted and another created, the modification time tells those two apart
        self._created: tuple[int, int, int] | None = None
        self._acquired = False             # this object made the file, whether or not its details were read

    def exists(self) -> bool:
        return self.path.exists()

    def touch(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()

    def acquire(self) -> bool:
        """Create the marker with `O_CREAT|O_EXCL`: True when this call made it, False when it was there.

        `exists()` followed by `touch()` lets two paths both conclude that the window is theirs; the exclusive
        create has exactly one winner. The file gets the signature line and its identity is remembered:
        `remove()` leaves a file that is no longer this one alone.

        SIGTERM and SIGINT are held back from the create to the moment the identity is recorded: a handler that
        raises there would leave a marker nobody owns, and an unsigned (still empty) one is a maintenance window
        an operator opened. A signal that arrived meanwhile is delivered when the mask is lifted, with the
        identity recorded (kit 2.2f, 4b)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        held = _hold_termination()
        try:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
            except FileExistsError:
                return False
            self._acquired = True
            try:
                try:
                    line = f"{KIT_MARKER_WORD} pid={os.getpid()} start={process_start_ticks(os.getpid()) or 0} " \
                           f"at={int(time.time())}\n"
                    os.write(fd, line.encode("ascii"))
                except OSError:
                    pass                   # an unsigned marker is an operator's: it errs toward standing down
                try:
                    info = os.fstat(fd)    # after the write: its modification time is the one remembered
                    self._created = (info.st_dev, info.st_ino, info.st_mtime_ns)
                except OSError:
                    self._created = None
            finally:
                os.close(fd)
            return True
        finally:
            _release_termination(held)

    @property
    def acquired(self) -> bool:
        """True when this object created the file (and so is the one that may remove it)."""
        return self._acquired

    def _signature(self) -> tuple[tuple[int, int, int], int, int] | None:
        """((device, inode, mtime), pid, start) of a regular, signed marker; None for anything else."""
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 512:
                return None
            head = os.read(fd, 512).decode("ascii", "replace")
        except OSError:
            return None
        finally:
            os.close(fd)
        m = _KIT_MARKER.match(head)
        return ((info.st_dev, info.st_ino, info.st_mtime_ns), int(m.group(1)), int(m.group(2))) if m else None

    def stale_kit_marker(self) -> bool:
        """True when the marker is one a kit run signed and that run no longer exists."""
        sig = self._signature()
        return sig is not None and not _kit_run_alive(sig[1], sig[2])

    def reclaim_stale(self) -> bool:
        """Remove a stale kit marker (see `stale_kit_marker`); True when it was removed. An operator's marker,
        a live run's marker and anything that is not a regular file are left as they are; the file is unlinked
        only if it is still the one that was read: same device, inode AND modification time, so an operator who
        overwrote it in place while the liveness of the run was being asked keeps the window he opened (kit 2.2f,
        4a)."""
        sig = self._signature()
        if sig is None or _kit_run_alive(sig[1], sig[2]):
            return False
        try:
            info = os.lstat(self.path)
            if (info.st_dev, info.st_ino, info.st_mtime_ns) != sig[0]:
                return False
            os.unlink(self.path)
        except OSError:
            return False
        return True

    def remove(self) -> None:
        """Remove the marker. A marker this object created is removed only while it is still that file (same
        device, inode and modification time): one an operator put in its place is theirs. When the file was
        created here but its details could not be read there is nothing to compare with: it is left in place and
        that is said (a signed marker of a run that is gone is reclaimed later), never deleted blind (kit 2.2f,
        4c)."""
        if self._acquired and self._created is None:
            self._acquired = False
            if self.path.exists():
                sys.stderr.write(f"openclaw: {self.path} was created by this run but its identity could not be "
                                 f"read; it is left in place (a signed marker of a finished run is reclaimed)\n")
            return
        if self._created is not None:
            created, self._created = self._created, None
            self._acquired = False
            try:
                info = os.lstat(self.path)
            except OSError:
                return
            if (info.st_dev, info.st_ino, info.st_mtime_ns) != created:
                return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class WindowBusy(Exception):
    """A maintenance window (`watchdog.off`) is already open, or another restart path holds the lock."""


class RestartLockUnusable(OSError):
    """The restart lock file cannot be opened (a symlink, a directory, no permission)."""


SELF_CGROUP_PATH = "/proc/self/cgroup"


def gateway_cgroup_state(self_cgroup_path: Path | str | None = None) -> str:
    """"inside", "outside" or "unknown" for this process and the gateway unit's cgroup.

    No such file (a host without procfs: nothing to be inside of) is "outside"; a file that exists and cannot
    be read or decoded is "unknown", and the restart paths refuse on it (kit 2.2c, 3e): a process that cannot
    tell must not stop the unit it may be a child of."""
    try:
        text = Path(self_cgroup_path or SELF_CGROUP_PATH).read_text(encoding="utf-8")
    except FileNotFoundError:
        return "outside"
    except (OSError, UnicodeDecodeError):
        return "unknown"
    return "inside" if GATEWAY_UNIT in text else "outside"


def inside_gateway_cgroup(self_cgroup_path: Path | str | None = None) -> bool:
    """True when this process is a child of the gateway unit: stopping or restarting the unit would kill
    the very process that has to finish the job (T29)."""
    return gateway_cgroup_state(self_cgroup_path) == "inside"


CGROUP_UNKNOWN_LINE = ("refusing: cannot tell whether this process runs inside the gateway cgroup "
                       "(/proc/self/cgroup is unreadable); nothing was stopped or restarted. Run this from "
                       "outside the gateway once the file can be read.")


def acquire_restart_lock(path: Path | None = None) -> int | None:
    """Take the ONE non-blocking exclusive lock every path that stops or restarts the gateway shares (the
    graceful restart, the drained window of `doctor` and `apply-pending`, setup's and `models update`'s
    restart). Returns the open descriptor, or None when another path holds it; RestartLockUnusable when the file
    cannot be opened (it is opened with O_NOFOLLOW: a symlink at that name is refused, never followed).

    The file is never unlinked: removing it while it is locked lets a process that already opened the old
    inode and one that creates a new file both hold "the" lock at once."""
    import fcntl
    path = path or LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as e:
        what = "is a symlink" if path.is_symlink() else type(e).__name__
        raise RestartLockUnusable(f"{path} {what}" if what == "is a symlink" else f"{path}: {what}") from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def release_restart_lock(fd: int | None) -> None:
    import fcntl
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def doctor(runner: Runner = default_runner, *, force: bool = False, dry_run: bool = False,
           even_if_busy: bool = False, busy_probe: Callable[[], "int | None"] | None = None,
           cleanup_sessions: bool = False, drain_timeout: float = 90, drain_interval: float = 3,
           health_timeout: float = 120, health_interval: float = 5,
           sleep: Callable[[float], None] = time.sleep, marker: Marker | None = None,
           openclaw_bin: str | None = None, out: Callable[[str], None] = print,
           self_cgroup_path: Path | str = "/proc/self/cgroup") -> int:
    """`openclaw doctor --fix`, the only safe way, implemented once.

    `doctor --fix` stops the gateway and re-inspects the stopped unit; if a child (claude,
    engram, npx) is still alive it aborts with "ownership or manager identity changed" and
    leaves the gateway DOWN (T01, lost half an hour on 2026-09-18). So the sequence is:

        touch watchdog.off  ->  stop  ->  poll until the cgroup drains  ->  doctor --fix
        ->  rm watchdog.off  ->  start  ->  poll `openclaw health`

    The drain is polled, never a fixed sleep. `doctor --fix` runs ONLY if it drained. The marker
    is removed and the gateway started again on EVERY exit path, including an exception or a
    Ctrl-C mid-sequence: a window that leaves the watchdog paused or the gateway stopped is the
    failure this function exists to prevent.

    Exit codes: 0 ok; 2 refused (marker present); 3 not drained; 4 doctor failed; 5 gateway did
    not come back; 6 refused (agent runs in flight, or the busy probe could not tell).

    The stop aborts every run in flight, so the doctor refuses a busy gateway before anything is
    stopped unless `even_if_busy` (`--even-if-busy`) says the operator accepts that (E4, ADR-0004).
    """
    oc = openclaw_bin or resolve_openclaw_bin()

    steps = ["touch watchdog.off", f"systemctl --user stop {GATEWAY_UNIT}",
             f"poll TasksCurrent until [not set] (up to {drain_timeout:g}s)", f"{oc} doctor --fix"]
    if cleanup_sessions:
        steps.append(f"{oc} sessions cleanup --all-agents")
    steps += ["remove watchdog.off", f"systemctl --user start {GATEWAY_UNIT}",
              f"poll {oc} health (up to {health_timeout:g}s)"]
    if dry_run:
        out("dry run -- would do, in order:")
        for i, step in enumerate(steps, 1):
            out(f"  {i}. {step}")
        return EXIT_OK

    env = systemd_env()

    def run_doctor() -> bool:
        rc, text = runner([oc, "doctor", "--fix"], env=env, timeout=DOCTOR_TIMEOUT)
        out(text)
        doctor_ok = rc == 0 and "Doctor complete" in text
        out(f"doctor={'ok' if doctor_ok else 'failed'}")
        if cleanup_sessions:
            _rc, cleaned = runner([oc, "sessions", "cleanup", "--all-agents"], env=env, timeout=300)
            out("sessions cleanup: " + " ".join(cleaned.splitlines()[-2:]))
        return doctor_ok

    return drained_window(run_doctor, runner=runner, force=force, require_idle=not even_if_busy,
                          busy_probe=busy_probe, drain_timeout=drain_timeout,
                          drain_interval=drain_interval, health_timeout=health_timeout,
                          health_interval=health_interval, sleep=sleep, marker=marker,
                          openclaw_bin=oc, out=out, self_cgroup_path=self_cgroup_path,
                          skip_message=f"doctor=skipped (the cgroup did not drain in {drain_timeout:g}s)")


HEALTH_PROBE_TIMEOUT_S = 60      # one `openclaw health` probe, at most
HEALTH_FINAL_PROBE_MIN_S = 5     # the last probe of a poll may start with less than this left: it gets this much


def poll_health(runner: Runner, oc: str, *, env: dict | None = None, timeout: float = 120,
                interval: float = 5, sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic) -> bool:
    """Poll `openclaw health` until it answers or `timeout` is spent. It sleeps BEFORE each probe: the
    caller has just started or restarted the gateway, so an immediate probe would only fail.

    `timeout` is a wall-clock budget on `clock` (monotonic) that INCLUDES the probes: each sleep is
    clipped to what is left and each probe's own timeout is clipped to it, so a hung `openclaw health`
    (rc 124 after its timeout) cannot stretch the poll. The call returns within `timeout` plus at most
    HEALTH_FINAL_PROBE_MIN_S. The iteration count stays as a second bound for a clock that does not move.

    Extracted from `drained_window` unchanged in behaviour; `graceful_restart` shares it (ADR-0004).
    """
    deadline = clock() + timeout
    for _ in range(max(1, int(timeout // interval))):
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(interval, remaining))
        left = deadline - clock()
        probe_timeout = min(HEALTH_PROBE_TIMEOUT_S, max(left, HEALTH_FINAL_PROBE_MIN_S))
        rc, _text = runner([oc, "health"], env=env, timeout=probe_timeout)
        if rc == 0:
            return True
    return False


EXIT_REFUSED_BUSY = 6     # require_idle was set and agent runs are in flight (or the probe failed)


def drained_window(action: Callable[[], bool], *, runner: Runner = default_runner, force: bool = False,
                   require_idle: bool = False, busy_probe: Callable[[], "int | None"] | None = None,
                   drain_timeout: float = 90, drain_interval: float = 3,
                   health_timeout: float = 120, health_interval: float = 5,
                   sleep: Callable[[float], None] = time.sleep, marker: Marker | None = None,
                   openclaw_bin: str | None = None, out: Callable[[str], None] = print,
                   skip_message: str = "", self_cgroup_path: Path | str = "/proc/self/cgroup",
                   lock_path: Path | None = None) -> int:
    """The one maintenance window (T01), shared by `doctor` and the kit's restart-required config applies.

        touch watchdog.off  ->  stop  ->  poll until the cgroup drains  ->  action()
        ->  rm watchdog.off  ->  start  ->  poll `openclaw health`

    `action` runs ONLY once the unit drained, and returns True on success. The marker is removed
    and the gateway started again on EVERY exit path, `action` raising included. With
    `require_idle` the window is refused, before anything is stopped, when the strict busy probe
    reports runs in flight or cannot tell (ADR-0001: defer, never force).

    Exit codes: 0 ok; 2 refused (marker present, or the restart lock is held by another path); 3 not
    drained; 4 action failed; 5 gateway did not come back; 6 refused (busy); 8 refused (this process is a
    child of the gateway: stopping the unit would kill the very process that has to start it again, T29).
    """
    marker = marker or Marker()
    env = systemd_env()
    oc = openclaw_bin or resolve_openclaw_bin()
    sysctl = ["systemctl", "--user"]

    cg = gateway_cgroup_state(self_cgroup_path)
    if cg == "inside":
        out("refusing: this process runs inside the gateway cgroup; stopping the gateway would kill it (T29). "
            "Run this from outside the gateway (a terminal, not an agent session).")
        return EXIT_REFUSED_SELF
    if cg == "unknown":
        out(CGROUP_UNKNOWN_LINE)
        return EXIT_REFUSED_SELF

    # One lock for every path that stops or restarts the unit: a second one refuses, with one line.
    try:
        lock_fd = acquire_restart_lock(lock_path)
    except RestartLockUnusable as e:
        out(f"refusing: the restart lock file cannot be used ({e}); remove it or fix its permissions; "
            "nothing was stopped.")
        return EXIT_REFUSED
    if lock_fd is None:
        out("refusing: another restart or maintenance window is in progress (the restart lock is held); "
            "nothing was stopped.")
        return EXIT_REFUSED
    try:
        return _drained_window_locked(action, runner=runner, force=force, require_idle=require_idle,
                                      busy_probe=busy_probe, drain_timeout=drain_timeout,
                                      drain_interval=drain_interval, health_timeout=health_timeout,
                                      health_interval=health_interval, sleep=sleep, marker=marker, env=env,
                                      oc=oc, sysctl=sysctl, out=out, skip_message=skip_message)
    finally:
        release_restart_lock(lock_fd)


def _drained_window_locked(action: Callable[[], bool], *, runner: Runner, force: bool, require_idle: bool,
                           busy_probe: Callable[[], "int | None"] | None, drain_timeout: float,
                           drain_interval: float, health_timeout: float, health_interval: float,
                           sleep: Callable[[float], None], marker: Marker, env: dict, oc: str,
                           sysctl: list[str], out: Callable[[str], None], skip_message: str) -> int:
    if marker.exists() and not force:
        out(f"refusing: {marker.path} exists, so another maintenance window is open. "
            "Pass --force if that window is dead.")
        return EXIT_REFUSED
    if require_idle:
        probe = busy_probe or (lambda: gateway_busy_strict(runner))
        busy = probe()
        if busy is None or busy > 0:
            out("refusing: " + ("the busy probe could not tell whether agent runs are in flight"
                                if busy is None else f"{busy} agent run(s) are in flight") + "; nothing was stopped")
            return EXIT_REFUSED_BUSY

    drained = action_ok = False
    code = EXIT_OK
    # Exclusive create: a window that appeared since the check above (the operator's) is not ours to take.
    exclusive = getattr(marker, "acquire", None)
    if force or exclusive is None:
        marker.touch()
    elif not exclusive():
        out(f"refusing: {marker.path} appeared while the window was being opened, so another maintenance window "
            "is open; nothing was stopped.")
        return EXIT_REFUSED
    try:
        # TimeoutStopSec=330 on the gateway unit: allow for it.
        runner(sysctl + ["stop", GATEWAY_UNIT], env=env, timeout=400)
        polls = max(1, int(drain_timeout // drain_interval))
        for _ in range(polls):
            rc, text = runner(sysctl + ["show", GATEWAY_UNIT, "-p", "TasksCurrent", "--value"], env=env)
            if rc == 0 and text.strip() == "[not set]":
                drained = True
                break
            sleep(drain_interval)
        out(f"drained={'yes' if drained else 'no'}")
        if drained:
            action_ok = bool(action())
        elif skip_message:
            out(skip_message)
    finally:
        marker.remove()
        runner(sysctl + ["start", GATEWAY_UNIT], env=env, timeout=120)
        healthy = poll_health(runner, oc, env=env, timeout=health_timeout, interval=health_interval, sleep=sleep)
        out(f"healthy={'yes' if healthy else 'no'}")
        if not healthy:
            code = EXIT_NOT_HEALTHY
    if code:
        return code
    if not drained:
        return EXIT_NOT_DRAINED
    return EXIT_OK if action_ok else EXIT_DOCTOR_FAILED


EXIT_APPLY_REJECTED = 7   # the dry run was rejected, so nothing was stopped


def apply_drained(patch: dict, replace_paths: list[str] | None, *, apply_patch: Callable[..., "tuple[bool, str]"],
                  runner: Runner = default_runner, out: Callable[[str], None] = print,
                  require_idle: bool = True, **window: object) -> tuple[int, bool]:
    """Apply a patch that carries restart-required keys, inside a drained window. (exit code, written).

    Dry run first: a rejected patch aborts BEFORE anything is stopped. Then `drained_window` with
    `require_idle`: with the gateway stopped there is no live process for OpenClaw's own watcher to
    force-restart, and `openclaw config patch` writes the file without one (P0 spike, ADR-0003).
    """
    ok, text = apply_patch(patch, dry_run=True, replace_paths=replace_paths)
    if not ok:
        out(f"OpenClaw rejected the patch in a dry run; nothing was stopped or applied: {text[-300:]}")
        return EXIT_APPLY_REJECTED, False
    written = {"ok": False}

    def action() -> bool:
        done, msg = apply_patch(patch, replace_paths=replace_paths, allow_restart=True)
        written["ok"] = done
        out("config patch applied" if done else f"config patch failed: {msg[-300:]}")
        return done

    code = drained_window(action, runner=runner, require_idle=require_idle, out=out, **window)
    return code, written["ok"]


# --- the post-setup watch: revert what this run wrote if the gateway stops answering (ADR-0003) ---------------
#
# Started by setup as a transient user unit (`systemd-run --user`), never in the foreground: the loop is
# minutes long and an open tool call holds a gateway stop (T39). It is bounded, cancellable (stop the unit,
# or a newer setup run, or watchdog.off) and reverts at most once.
#
# 2.1.1 (D1, 2026-10-10): a probe that cannot RUN is not a failed gateway. The unit inherited the user
# manager's PATH, which has neither openclaw's directory nor node, so every `openclaw health` returned 127 and
# the watch "reverted" a healthy gateway. The verdict is now pure (`judge`), distinguishes four probe classes,
# waits for the reload to settle, needs a sustained failure AND a corroborating signal, and ends INCONCLUSIVE
# (nothing reverted, nothing queued, operator told) whenever it cannot tell.

WATCH_UNIT_PREFIX = "openclaw-config-watch-"
WATCH_WINDOW = 900.0            # seconds the unit watches after a setup run
WATCH_INTERVAL = 30.0
WATCH_FAILURES = 3              # the count rule `watch_config` falls back to when no `sustain_s` is given
WATCH_SETTLE_S = 120.0          # probes are ignored this long after the last reload of a recorded key
WATCH_SUSTAIN_S = 300.0         # the failure must last this long (after the settle) ...
WATCH_MAX_TICKS = 61            # hard bound on the loop, whatever window and interval say
GATEWAY_PORT = 18789
# ... and be corroborated. This is what the post-setup unit runs with (`cmd_config_watch`).
WATCH_POLICY: dict = {"settle_s": WATCH_SETTLE_S, "sustain_s": WATCH_SUSTAIN_S, "need_corroboration": True}
_TRANSIENT_STATES = frozenset({"activating", "deactivating", "reloading"})

PROBE_ANSWERED = "answered"        # rc 0
PROBE_NO_ANSWER = "no-answer"      # the CLI ran and the gateway did not answer
PROBE_SLOW = "slow"                # rc 124: a timeout. Counted as a failure only when corroborated
PROBE_CANNOT = "cannot-probe"      # rc 126/127, a missing interpreter: the PROBE failed, never the gateway

V_HEALTHY, V_WATCHING, V_FAILING, V_INCONCLUSIVE = "healthy", "watching", "failing", "inconclusive"

OUTCOMES = ("healthy", "reverted", "inconclusive", "revert-failed", "baseline-unhealthy")


class Probe(NamedTuple):
    t: float            # seconds since the watch started
    cls: str            # PROBE_*
    rc: int
    detail: str = ""    # first line of the output (never a secret: it is `openclaw health` text)


class Verdict(NamedTuple):
    kind: str
    reason: str = ""


def classify_probe(rc: int, text: str = "") -> str:
    """The class of one `openclaw health` call."""
    if rc == 0:
        return PROBE_ANSWERED
    if rc == 124:
        return PROBE_SLOW
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    if rc in (126, 127) or first.startswith("env: ") or "not found on PATH" in first:
        return PROBE_CANNOT
    return PROBE_NO_ANSWER


def failing_run(probes: list[Probe], settle_until: float = 0.0) -> list[Probe]:
    """The trailing run of probes that did not answer, counting only probes taken after the settle period."""
    run: list[Probe] = []
    for p in probes:
        if p.t < settle_until:
            continue
        if p.cls == PROBE_ANSWERED:
            run = []
        else:
            run.append(p)
    return run


def run_sustained(run: list[Probe], sustain_s: float | None, min_failures: int = WATCH_FAILURES) -> bool:
    """`sustain_s` None is the legacy rule: `min_failures` failed probes in a row."""
    if not run:
        return False
    if sustain_s is None:
        return len(run) >= min_failures
    return run[-1].t - run[0].t >= sustain_s


def judge(probes: list[Probe], *, settle_until: float = 0.0, sustain_s: float | None = None,
          min_failures: int = WATCH_FAILURES, need_corroboration: bool = False,
          corroborated: "bool | None" = None, baseline_healthy: bool = True,
          corroboration: str = "") -> Verdict:
    """The verdict on the probes so far. Pure: no clock, no I/O.

    healthy       the last counted probe answered (or nothing was counted yet)
    watching      failing, not yet for long enough
    failing       sustained failure that is corroborated (or the legacy count rule)
    inconclusive  the probe cannot run, the baseline was already failing, or the failure is uncorroborated
    """
    for p in probes:
        if p.cls == PROBE_CANNOT:
            return Verdict(V_INCONCLUSIVE, f"probe could not run: {p.detail or f'rc {p.rc}'}")
    run = failing_run(probes, settle_until)
    if not run:
        return Verdict(V_HEALTHY, "answering" if probes else "no probe yet")
    if not run_sustained(run, sustain_s, min_failures):
        return Verdict(V_WATCHING, f"{len(run)} failed probe(s), not sustained yet")
    if not baseline_healthy:
        return Verdict(V_INCONCLUSIVE, "baseline-unhealthy")
    if need_corroboration and not corroborated:
        return Verdict(V_INCONCLUSIVE, "the gateway did not answer, but nothing corroborates a failure "
                                       "(same process, port listening, no restart in the journal)")
    span = run[-1].t - run[0].t
    return Verdict(V_FAILING, f"no answer for {span:.0f}s" + (f"; {corroboration}" if corroboration else ""))


# --- what the gateway's own journal says about a reload (2026.9.9 wording) -----------------------------------------

class ReloadEvent(NamedTuple):
    epoch: float
    applied: bool          # "config hot reload applied" (True) or "config change detected" (False)
    paths: list


_RELOAD_RE = re.compile(r"\[reload\] config (change detected; evaluating reload|hot reload applied) \(([^)]*)\)")
_STAMP_RE = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:?\d\d)")
_CRASH_RE = re.compile(r"Main process exited|Failed with result|Scheduled restart job|Stopping openclaw-gateway|"
                       r"Stopped openclaw-gateway|Started openclaw-gateway")


def _line_epoch(line: str) -> float | None:
    """The epoch of a `journalctl -o short-iso` line (`2026-10-10T15:32:59+00:00 host unit[pid]: msg`)."""
    tok = line.split(None, 1)[0] if line.strip() else ""
    m = _STAMP_RE.fullmatch(tok)
    if not m:
        return None
    from datetime import datetime, timedelta, timezone
    y, mo, d, h, mi, sec = (int(x) for x in m.groups()[:6])
    zone = m.group(7)
    off = timedelta(0)
    if zone != "Z":
        digits = zone[1:].replace(":", "")
        off = timedelta(hours=int(digits[:2]), minutes=int(digits[2:])) * (1 if zone[0] == "+" else -1)
    try:
        return (datetime(y, mo, d, h, mi, sec, tzinfo=timezone.utc) - off).timestamp()
    except ValueError:
        return None


def parse_reload_events(lines: list[str]) -> list[ReloadEvent]:
    """Every reload line that carries a timestamp and a path list. A line it cannot read is skipped, never raised."""
    out: list[ReloadEvent] = []
    for line in lines:
        m = _RELOAD_RE.search(line)
        epoch = _line_epoch(line) if m else None
        if m and epoch is not None:
            paths = [p.strip() for p in m.group(2).split(",") if p.strip()]
            out.append(ReloadEvent(epoch, m.group(1).startswith("hot reload"), paths))
    return out


def _names(event_path: str, recorded: str) -> bool:
    return event_path == recorded or event_path.startswith(recorded + ".") or recorded.startswith(event_path + ".")


def last_reload_event(lines: list[str], paths: list[str]) -> "ReloadEvent | None":
    """The latest reload line (detected or applied) that names one of the recorded paths."""
    hits = [e for e in parse_reload_events(lines) if any(_names(p, r) for p in e.paths for r in paths)]
    return max(hits, key=lambda e: e.epoch) if hits else None


def applied_paths(lines: list[str], paths: list[str]) -> dict[str, float]:
    """{recorded path: epoch of its latest `config hot reload applied` line}."""
    out: dict[str, float] = {}
    for e in parse_reload_events(lines):
        if not e.applied:
            continue
        for r in paths:
            if any(_names(p, r) for p in e.paths):
                out[r] = max(out.get(r, 0.0), e.epoch)
    return out


def journal_shows_crash(lines: list[str], *, since: float) -> bool:
    """A systemd stop/restart/failure line for the gateway unit at or after `since` (epoch)."""
    for line in lines:
        epoch = _line_epoch(line)
        if epoch is not None and epoch >= since and _CRASH_RE.search(line):
            return True
    return False


# --- the unit's own environment (the primary D1 fix) --------------------------------------------------------------

def unit_path(*, openclaw: str | None = None, which: Callable[[str], "str | None"] = shutil.which) -> str:
    """PATH for a transient unit, in the order of scripts/openclaw/_common.sh.

    A `systemd-run --user` unit inherits the USER MANAGER's environment, not the login shell's, so
    `/home/linuxbrew/.linuxbrew/bin` (openclaw and node) is not on it and `#!/usr/bin/env node` fails with 127.
    Order: OPENCLAW_EXTRA_PATH (the operator's choice, process env then kit-host.env), the directory of the
    resolved openclaw, the directory of the node it runs on, brew's bin, then the system directories. A
    version manager's ephemeral directory (`_EPHEMERAL_PATH`) is never put on it.
    """
    oc = openclaw or resolve_openclaw_bin()
    parts: list[str] = []
    extra = os.environ.get("OPENCLAW_EXTRA_PATH") or read_host_env().get("OPENCLAW_EXTRA_PATH", "")
    parts += [p for p in extra.split(":") if p]
    if os.path.isabs(oc):
        parts.append(os.path.dirname(oc))
        try:
            bundled = Path(os.path.realpath(oc)).parent / "node"
        except OSError:
            bundled = None
        if bundled is not None and bundled.is_file():
            parts.append(str(bundled.parent))
    brew_node = Path("/home/linuxbrew/.linuxbrew/opt/node/bin/node")
    node = which("node")
    if node:
        parts.append(os.path.dirname(node))
    elif brew_node.is_file():
        parts.append(str(brew_node.parent))
    parts += ["/home/linuxbrew/.linuxbrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p in seen or any(bad in p for bad in _EPHEMERAL_PATH):
            continue
        seen.add(p)
        out.append(p)
    return ":".join(out)


def _with_unit_path() -> "Callable[[], None]":
    """Prepend `unit_path()` to this process's PATH (ephemeral entries dropped); returns the restore function."""
    before = os.environ.get("PATH")
    existing = [p for p in (before or "").split(":") if p and not any(bad in p for bad in _EPHEMERAL_PATH)]
    merged: list[str] = []
    for p in unit_path().split(":") + existing:
        if p not in merged:
            merged.append(p)
    os.environ["PATH"] = ":".join(merged)

    def restore() -> None:
        if before is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = before
    return restore


def notify_owner(message: str, runner: Runner = default_runner) -> bool:
    """Telegram to the operator, as the kit's shell scripts do (`openclaw message send`). Best effort."""
    target = read_host_env().get("OPENCLAW_OWNER_TELEGRAM_ID", "")
    if not target:
        return False
    rc, _out = runner([resolve_openclaw_bin(), "message", "send", "--channel", "telegram", "--target", target,
                       "--message", message], timeout=30)
    return rc == 0


def _unit_state(runner: Runner) -> str:
    """ActiveState of the gateway unit ("" when unreadable)."""
    rc, text = runner(["systemctl", "--user", "show", GATEWAY_UNIT, "-p", "ActiveState", "--value"],
                      env=systemd_env())
    return text.strip() if rc == 0 else ""


def _unit_props(runner: Runner) -> "dict[str, str] | None":
    rc, text = runner(["systemctl", "--user", "show", GATEWAY_UNIT, "-p", "MainPID,NRestarts,ActiveState"],
                      env=systemd_env(), timeout=15)
    if rc != 0:
        return None
    props: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key in ("MainPID", "NRestarts", "ActiveState"):
            props[key] = value.strip()
    return props or None


def _port_listening(runner: Runner, port: int) -> "bool | None":
    rc, text = runner(["ss", "-H", "-ltn", f"sport = :{port}"], timeout=10)
    return bool(text.strip()) if rc == 0 else None


def _gateway_journal(runner: Runner, since_epoch: float) -> "list[str] | None":
    """A bounded read of the gateway unit's journal; None when unreadable (the watch then settles on time alone)."""
    rc, text = runner(["journalctl", "--user", "-u", GATEWAY_UNIT, f"--since=@{int(since_epoch)}", "--no-pager",
                       "-o", "short-iso", "-n", "400"], env=systemd_env(), timeout=20)
    return text.splitlines() if rc == 0 else None


def _corroboration(runner: Runner, base: "dict[str, str] | None", journal: "list[str] | None", *,
                   since: float, port: int) -> "tuple[bool | None, str]":
    """(True | False | None for unknown, why). Any one signal that the gateway really went down is enough."""
    reasons: list[str] = []
    readable = False
    props = _unit_props(runner)
    if props:
        readable = True
        if props.get("ActiveState") in ("failed", "inactive"):
            reasons.append(f"unit {props['ActiveState']}")
        if base and base.get("MainPID") and props.get("MainPID") != base["MainPID"]:
            reasons.append("MainPID changed")
        try:
            if base and int(props.get("NRestarts", "0") or 0) > int(base.get("NRestarts", "0") or 0):
                reasons.append("the unit restarted")
        except ValueError:
            pass
    listening = _port_listening(runner, port)
    if listening is not None:
        readable = True
        if listening is False:
            reasons.append(f"port {port} not listening")
    if journal is not None:
        readable = True
        if journal_shows_crash(journal, since=since):
            reasons.append("the journal shows a stop or restart after the change")
    if reasons:
        return True, ", ".join(reasons)
    return (False if readable else None), ""


def _first_line(text: str) -> str:
    text = (text or "").strip()
    return text.splitlines()[0][:160] if text else ""


def watch_config(run_id: str, *, runner: Runner = default_runner,
                 sleep: Callable[[float], None] = time.sleep, window: float = WATCH_WINDOW,
                 interval: float = WATCH_INTERVAL, failures: int = WATCH_FAILURES,
                 load: Callable[[], object] | None = None, save: Callable[[object], None] | None = None,
                 apply_patch: Callable[..., "tuple[bool, str]"] | None = None,
                 notify: Callable[[str], bool] | None = None, marker: Marker | None = None,
                 reload_mode: Callable[[], "str | None"] | None = None,
                 load_doc: Callable[[], "dict | None"] | None = None,
                 out: Callable[[str], None] = print, settle_s: float = 0.0, sustain_s: float | None = None,
                 need_corroboration: bool = False, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, **window_kw: object) -> int:
    """Watch the gateway for `window` seconds after a setup run; revert that run's changes if it really failed.

    Exit 0 whether or not it reverted (the unit's job is done); 1 when the revert itself failed.

    The defaults keep the original rule (`failures` failed probes in a row, no settle, no corroboration);
    the post-setup unit passes `WATCH_POLICY`: settle 120 s after the last reload, a failure sustained for
    300 s, and a corroborating signal (the process changed, the unit restarted or is down, the port is closed,
    the journal shows a stop). A probe that cannot run (127, a missing node) ends the watch INCONCLUSIVE
    without counting: nothing is reverted and nothing is queued.

    A tick that finds no `config_watch` for this run id (cancelled or superseded), `done` already
    set, or `watchdog.off` present (a maintenance window is open) ends the watch without a revert.
    A gateway that is activating, deactivating or reloading is neither a failure nor a success: a
    restart by OpenClaw, the watchdog or a kit window is in progress. On a corroborated failure hot keys are
    reverted at once with no restart; restart-required keys only inside the kit's drained window (a blind
    `config patch` of such a key would make OpenClaw force a restart over live runs after 300 s).
    """
    from .setup import state as setup_state
    from .setup.cockpits import openclaw as cockpit

    load = load or setup_state.load
    save = save or _watch_save
    apply_patch = apply_patch or cockpit.apply_patch
    notify = notify or (lambda msg: notify_owner(msg, runner))
    reload_mode = reload_mode or cockpit._live_reload_mode
    marker = marker or Marker()
    oc = resolve_openclaw_bin()
    ticks = min(WATCH_MAX_TICKS, int(window // interval) + 1)
    t0, w0 = clock(), wall()
    probes: list[Probe] = []
    base = _unit_props(runner) if need_corroboration else None
    journal: "list[str] | None" = None
    version_checked = False
    port = GATEWAY_PORT
    if load_doc is not None:
        try:
            port = int(((load_doc() or {}).get("gateway") or {}).get("port") or GATEWAY_PORT)
        except (TypeError, ValueError):
            port = GATEWAY_PORT
    for tick in range(ticks):
        if tick:
            sleep(interval)
        s = load()
        cw = s.openclaw.config_watch
        if not cw or cw.get("run_id") != run_id or cw.get("done"):
            out("watch: cancelled, superseded or already finished; nothing to do")
            return 0
        if marker.exists():
            out("watch: a maintenance window is open (watchdog.off); standing down")
            return 0
        if _unit_state(runner) in _TRANSIENT_STATES:
            continue
        recorded = _recorded_paths(cw)
        if not version_checked:
            # The precondition: the probe's own tool must run at all, or no probe means anything.
            version_checked = True
            vrc, vtext = runner([oc, "--version"], timeout=30)
            if vrc != 0:
                return _finish_inconclusive(
                    s, cw, f"probe could not run: `openclaw --version` failed (rc {vrc}): {_first_line(vtext)}",
                    save=save, notify=notify, out=out)
        elapsed = clock() - t0
        rc, text = runner([oc, "health"], timeout=HEALTH_PROBE_TIMEOUT_S)
        cls = classify_probe(rc, text)
        probes.append(Probe(elapsed, cls, rc, _first_line(text)))
        out(f"tick={tick} rc={rc} class={cls} elapsed={elapsed:.0f}s")
        if cls == PROBE_CANNOT:
            return _finish_inconclusive(s, cw, f"probe could not run: {_first_line(text) or f'rc {rc}'}",
                                        save=save, notify=notify, out=out)
        if cls == PROBE_ANSWERED:
            continue
        # A failing probe: settle against the gateway's own reload lines, then ask for corroboration.
        settle_until = settle_s
        if settle_s or need_corroboration:
            journal = _gateway_journal(runner, w0 - 600)
            ev = last_reload_event(journal or [], recorded)
            if ev is not None:
                settle_until = max(settle_until, (ev.epoch - w0) + settle_s)
        run = failing_run(probes, settle_until)
        corroborated, why = None, ""
        if need_corroboration and run_sustained(run, sustain_s, failures):
            corroborated, why = _corroboration(runner, base, journal, since=w0, port=port)
        verdict = judge(probes, settle_until=settle_until, sustain_s=sustain_s, min_failures=failures,
                        need_corroboration=need_corroboration, corroborated=corroborated,
                        baseline_healthy=cw.get("baseline_healthy") is not False, corroboration=why)
        if verdict.kind == V_INCONCLUSIVE and verdict.reason == "baseline-unhealthy":
            # The gateway was already failing before this run wrote anything (E2): the writes are not the
            # cause, so reverting them would only add change. Say so once and stop.
            cw["done"] = True
            cw["result"] = {"outcome": "baseline-unhealthy", "reason": "baseline-unhealthy", "hot": [],
                            "restart": [], "problems": [], "kept_confirmed": [], "skipped": "baseline-unhealthy"}
            save(s)
            message = ("OpenClaw is not answering `openclaw health`, but it was already failing before the "
                       "last `ai-resources setup` wrote anything, so the kit did not revert that run. "
                       "Check `ai-resources openclaw status`.")
            out(message)
            if not notify(message):
                out("watch: no Telegram target configured or the send failed; the journal has the details")
            return 0
        if verdict.kind == V_INCONCLUSIVE:
            return _finish_inconclusive(s, cw, verdict.reason, save=save, notify=notify, out=out)
        if verdict.kind == V_FAILING:
            answered = [p.t for p in probes if p.cls == PROBE_ANSWERED]
            kept = sorted(p for p, epoch in applied_paths(journal or [], recorded).items()
                          if any(t >= epoch - w0 for t in answered))
            return _revert_run(s, cw, runner=runner, sleep=sleep, save=save, apply_patch=apply_patch, notify=notify,
                               reload_mode=reload_mode, marker=marker, out=out, load_doc=load_doc, kept=kept,
                               reason=verdict.reason, corroborated=bool(corroborated), **window_kw)
    trailing = failing_run(probes, 0.0)
    s = load()
    cw = s.openclaw.config_watch
    ours = bool(cw and cw.get("run_id") == run_id and not cw.get("done"))
    if trailing and ours:
        return _finish_inconclusive(
            s, cw, f"the gateway was not answering at the end of the watch ({len(trailing)} probe(s)) but the "
                   "failure was not sustained long enough to act on", save=save, notify=notify, out=out)
    if ours and not trailing:
        # Close the record (T4): a healthy window is a finished watch, not one that looks like it is still running.
        cw["done"] = True
        cw["result"] = {"outcome": "healthy", "reason": "", "hot": [], "restart": [], "problems": [],
                        "kept_confirmed": []}
        save(s)
    out("watch: the gateway stayed healthy; nothing to revert")
    return 0


WATCH_SAVE_WAIT_S = 60.0       # how long the watch waits for the setup-state lock when it records its result


def _watch_save(s) -> None:
    """Record the watch's result without overwriting what another run wrote while the watch (or its revert)
    was running. The state object `s` is minutes old; the lock is NOT held for those minutes. Instead the
    state is loaded again under the lock, immediately before the save, and only what the watch owns is
    written: its own `config_watch` record (when it is still this run's) and the restore entries it queued."""
    from .setup import state as setup_state
    mine = s.openclaw.config_watch
    owned_pending = list(getattr(s, "_owned_pending", []))

    def apply(fresh) -> bool:
        cur = fresh.openclaw.config_watch
        if not mine or not cur or cur.get("run_id") != mine.get("run_id"):
            return False                  # cancelled, superseded or torn down meanwhile: nothing of ours to write
        fresh.openclaw.config_watch = mine
        known = {(e.get("op"), e.get("path"), e.get("source")) for e in fresh.openclaw.host_restart_pending}
        for entry in owned_pending:
            if (entry.get("op"), entry.get("path"), entry.get("source")) not in known:
                fresh.openclaw.host_restart_pending.append(entry)
        return True

    if not setup_state.update(apply, wait_s=WATCH_SAVE_WAIT_S):
        print("watch: the setup-state lock is busy or unusable; the result of the watch was not recorded")
        # 2.2c (6c): tell the operator through a tiny marker next to the state, which `status` and `verify` read
        result = (mine or {}).get("result") or {}
        setup_state.write_watch_marker({"run_id": (mine or {}).get("run_id", ""),
                                        "outcome": result.get("outcome", "") or "unknown",
                                        "reason": result.get("reason", "")})


def _recorded_paths(cw: dict) -> list[str]:
    paths: list[str] = []
    for ch in cw.get("changes") or []:
        for p in (".".join(ch["path"]), ".".join(restore_path_of(ch))):
            if p not in paths:
                paths.append(p)
    return paths


def _finish_inconclusive(s, cw: dict, reason: str, *, save, notify, out, kept: list[str] | None = None) -> int:
    """End the watch having verified nothing: no revert, no restore entry, one notice. Exit 0."""
    cw["done"] = True
    cw["result"] = {"outcome": "inconclusive", "reason": reason, "hot": [], "restart": [], "problems": [],
                    "kept_confirmed": sorted(kept or [])}
    save(s)
    message = (f"The post-setup watch could not tell whether the last `ai-resources setup` broke the gateway "
               f"({reason}); it reverted nothing and queued nothing. Check `ai-resources openclaw status`.")
    out(message)
    if not notify(message):
        out("watch: no Telegram target configured or the send failed; the journal has the details")
    return 0


def _route_restart(changes: list[dict], patch: dict, ruleset: "openclaw_reload_rules.RuleSet | None",
                   mode: "str | None") -> list[str]:
    """The leaf paths of the restore patch that need the drained window.

    The kind recorded when the key was written is used when the live version cannot be read or is the one
    the record was made on: re-classifying in a degraded environment is what made D1 queue hot keys. A record
    from before 2.1.1 (no kind) and a different readable version are classified with the live reload rules,
    which fail closed when they cannot be read.
    """
    rr = openclaw_reload_rules
    version = ruleset.version if ruleset is not None else None
    live = set(rr.restart_paths(patch, ruleset=ruleset, reload_mode=mode))
    by_path: dict[str, dict] = {}
    for ch in changes:
        for key in (".".join(ch["path"]), ".".join(restore_path_of(ch))):
            by_path.setdefault(key, ch)
    restart: list[str] = []
    for leaf in rr.leaf_paths(patch):
        ch = by_path.get(leaf) or next((c for k, c in by_path.items()
                                        if leaf.startswith(k + ".") or k.startswith(leaf + ".")), None)
        kind = ch.get("kind") if ch else None
        trusted = (kind in ("hot", "restart", "none") and mode != "off"
                   and (version is None or ch.get("openclaw_version") in (None, version)))
        is_restart = (kind == "restart") if trusted else (leaf in live)
        if is_restart:
            restart.append(leaf)
    return restart


def _revert_run(s, cw: dict, *, runner: Runner, sleep, save, apply_patch, notify, reload_mode, marker, out,
                load_doc=None, kept: list[str] | None = None, reason: str = "", corroborated: bool = False,
                **window_kw) -> int:
    rr = openclaw_reload_rules

    kept = list(kept or [])
    changes = [ch for ch in (cw.get("changes") or [])
               if not any(_names(".".join(ch["path"]), k) for k in kept)]
    if kept and not changes:
        return _finish_inconclusive(
            s, cw, "every recorded key was hot-applied and the gateway answered afterwards, so the later failure "
                   "is not attributed to this run", save=save, notify=notify, out=out, kept=kept)
    patch, rp = restore_patch(changes, load_doc() if load_doc else None)
    ruleset = rr.current_ruleset(lambda argv, **kw: runner([resolve_openclaw_bin(), *argv[1:]], **kw))
    restart = _route_restart(changes, patch, ruleset, reload_mode())
    hot, res = rr.split_patch(patch, restart)
    hot_rp, res_rp = rr.split_replace_paths(rp, restart)
    hot_paths, res_paths = rr.leaf_paths(hot), rr.leaf_paths(res)
    problems: list[str] = []
    rejected = ""
    done_hot = done_res = False
    out("watch: the gateway stopped answering"
        + (f" ({reason})" if reason else "") + "; reverting this run's config changes")
    if hot:
        try:
            ok, text = apply_patch(hot, dry_run=True, replace_paths=hot_rp)
            if ok:
                ok, text = apply_patch(hot, replace_paths=hot_rp)
        except rr.RestartRequired as e:       # defensive: the same table that split the patch refused it
            ok, text = False, str(e)
        done_hot = ok
        if not ok:
            problems.append(f"hot keys not reverted: {text[-200:]}")
    if res:
        code, written = apply_drained(res, res_rp, apply_patch=apply_patch, runner=runner, out=out,
                                      require_idle=False, sleep=sleep, marker=marker, **window_kw)
        done_res = written
        if code == EXIT_APPLY_REJECTED:
            # The dry run was rejected, so nothing was stopped and nothing is known about the keys: never queue a
            # restore for it (D1 c). The reason is reported; the operator decides.
            rejected = f"the drained-window dry run was rejected (exit {code}); nothing was stopped or queued"
        elif not written:
            problems.append(f"restart-required keys not reverted (drained window exit {code})")
            for ch in changes:
                if ".".join(restore_path_of(ch)) in res_paths:
                    entry = {"op": "restore", "path": ".".join(ch["path"]), "change": ch, "source": "watch",
                             "corroborated": corroborated, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
                    s.openclaw.host_restart_pending.append(entry)
                    # remembered as this code's own, so the merging save writes these and nothing else
                    s.__dict__.setdefault("_owned_pending", []).append(entry)
    if problems:
        outcome = "revert-failed"
    elif done_hot or done_res:
        outcome = "reverted"
    else:
        outcome = "inconclusive"
    cw["done"] = True
    cw["result"] = {"outcome": outcome, "reason": rejected or ("" if outcome != "inconclusive" else
                                                                "nothing needed reverting"),
                    "hot": hot_paths if done_hot else [], "restart": res_paths if done_res else [],
                    "problems": problems, "kept_confirmed": sorted(kept)}
    save(s)
    parts = []
    if done_hot:
        parts.append("reverted: " + ", ".join(hot_paths))
    if done_res:
        parts.append("reverted in a drained window (restart-required): " + ", ".join(res_paths))
    if rejected:
        parts.append(rejected)
    if kept:
        parts.append("kept (applied and answered afterwards): " + ", ".join(sorted(kept)))
    if problems:
        parts.append("; ".join(problems) + ". Check `ai-resources openclaw status`.")
    message = ("OpenClaw did not answer `openclaw health` after the last `ai-resources setup`, so the kit "
               "reverted what that run wrote. " + " | ".join(parts or ["nothing to revert"]))
    out(message)
    if not notify(message):
        out("watch: no Telegram target configured or the send failed; the journal has the details")
    return 1 if problems else 0


def restore_path_of(ch: dict) -> list[str]:
    return ch["path"] if ch.get("had") else ch.get("delete", ch["path"])


def cmd_config_watch(args: argparse.Namespace) -> int:
    import signal

    def _term(_signum, _frame):
        # systemd stops the unit with SIGTERM. Raise, so the `finally` of an open drained window runs:
        # watchdog.off removed and the gateway started again on EVERY exit path.
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, _term)
    from .setup.cockpits import openclaw as cockpit

    def load_doc():
        try:
            return cockpit.read_config(cockpit.config_path())
        except Exception:                      # an unreadable live file means "do not filter"
            return None

    # Belt and braces for D1: the unit was started with its own PATH, and this makes a unit started by an older
    # setup (or by hand) find openclaw and node as well. Restored on the way out so nothing leaks.
    restore = _with_unit_path()
    try:
        return watch_config(args.run_id, window=args.window, interval=args.interval, load_doc=load_doc,
                            **WATCH_POLICY)
    finally:
        restore()


# --- the restart guard: is a turn in flight right now? --------------------------------------------
#
# The drain above answers "did everything die after the stop". This answers the question before
# it. OpenClaw has no "turn in flight" query (`sessions list` returns store paths, `gateway
# status` service state), and CPU or transcript mtime gave two false "stuck agent" diagnoses, so
# the signal is the one the drain already trusts: what the unit's cgroup holds. Each agent turn
# is a CLI worker child of the gateway; a restart kills all of them.

# Two assumptions the probe makes, both of which fail OPEN (the restart proceeds, as it did before
# the guard existed) and neither of which is detectable from inside it:
#   * cgroup v2. The unit's tree is read from `<cgroup root><ControlGroup>/cgroup.procs`, the
#     unified hierarchy. On a v1 or hybrid host that path does not exist (or holds nothing), the
#     probe answers "idle" and the guard is silently absent; only the read-failure line through
#     `out` hints at it.
#   * argv[0] only. An agent counts when the basename of its own argv[0] is in AGENT_CLI_NAMES.
#     A CLI started through a node shim (`node .../cli.js`) or wrapped (`sudo claude`,
#     `env claude`) has another argv[0] and is invisible. That is deliberate: it is the same
#     rule that keeps the gateway's own node workers out of the count, and matching on later
#     arguments would let a path that merely contains "claude" make an idle gateway look busy.
#
# argv[0] basenames of the agent CLIs the gateway spawns for a turn. The gateway's own node
# workers (`spawn-broker`, `sqlite-readonly-location`, `dist/index.js` itself) are deliberately
# absent: they are always there, so counting them would make every gateway look busy.
AGENT_CLI_NAMES = frozenset({"claude", "codex", "gemini", "agy", "opencode"})
CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_ROOT = Path("/proc")


class AgentProc(NamedTuple):
    pid: int
    agent: str
    elapsed: float | None     # seconds since the process started; None when /proc/<pid>/stat is unreadable


def _elapsed(proc_root: Path, pid: int) -> float | None:
    """Seconds since `pid` started: /proc/uptime minus starttime (field 22 of stat, in clock ticks)."""
    try:
        stat = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        uptime = float((proc_root / "uptime").read_text(encoding="utf-8").split()[0])
        # `comm` (field 2) may hold spaces and parentheses: fields resume after the LAST ")".
        start = int(stat.rpartition(")")[2].split()[19])
        return max(0.0, uptime - start / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        return None


def _unit_pids(unit_dir: Path, out: Callable[[str], None] | None) -> list[int]:
    """Every pid in `unit_dir`'s cgroup and all its descendants, each once, in walk order.

    cgroup v2 lists only the processes directly in a cgroup in its `cgroup.procs`, while the
    drain above (`TasksCurrent`) counts the whole subtree. The unit is flat today; walking keeps
    the probe honest the day OpenClaw gains `Delegate=yes` or a scope per turn, instead of
    silently answering "idle" for work that lives one level down."""
    if not unit_dir.is_dir():
        # A v1 or hybrid host answers a ControlGroup that is not under the v2 root. `os.walk` would
        # swallow that and yield nothing, which reads as "idle" with no trace: say it instead.
        if out:
            out(f"restart guard: {unit_dir} is not a cgroup v2 directory; treating the gateway as idle")
        return []
    pids: list[int] = []
    seen: set[int] = set()

    def unreadable(e: OSError) -> None:
        if out and Path(e.filename or "") == unit_dir:
            out(f"restart guard: cannot read {unit_dir} ({e.strerror or e}); treating the gateway as idle")

    for here, _dirs, files in os.walk(unit_dir, onerror=unreadable):
        if "cgroup.procs" not in files:
            continue
        try:
            listing = (Path(here) / "cgroup.procs").read_text(encoding="utf-8").split()
        except OSError as e:
            if out and Path(here) == unit_dir:
                out(f"restart guard: cannot read {unit_dir}/cgroup.procs ({e}); treating the gateway as idle")
            continue
        # One at a time: a fork racing the read can list a pid twice within a single cgroup.procs.
        for p in listing:
            if p.isdigit() and int(p) not in seen:
                seen.add(int(p))
                pids.append(int(p))
    return pids


def gateway_busy(runner: Runner = default_runner, *, cgroup_root: Path = CGROUP_ROOT,
                 proc_root: Path = PROC_ROOT,
                 out: Callable[[str], None] | None = None) -> tuple[int, list[AgentProc]]:
    """(number of agent CLI workers in the gateway's cgroup tree, one AgentProc per worker).

    Any failure (no systemd, a stopped unit, an unreadable cgroup or /proc, a path that leaves
    the cgroup root) is "not busy": a probe that blocks an upgrade because it could not read
    /proc is worse than no probe, the rule the gateway guard hook states for itself.

    That rule makes a permanently broken probe look exactly like an idle host, so the failures
    that are NOT the normal "unit is stopped" case are also said once through `out` (a quiet
    line), which is how a dead guard gets noticed.
    """
    try:
        rc, group = runner(["systemctl", "--user", "show", GATEWAY_UNIT, "-p", "ControlGroup", "--value"],
                           env=systemd_env())
        group = group.strip()
        if rc != 0:
            if out:
                out(f"restart guard: systemctl could not describe {GATEWAY_UNIT} (rc {rc}); treating the gateway as idle")
            return 0, []
        if not group.startswith("/"):
            return 0, []   # a stopped unit has no cgroup: nothing can be in flight
        root = cgroup_root.resolve()
        unit_dir = (root / group.lstrip("/")).resolve()
        if not unit_dir.is_relative_to(root):
            return 0, []
        found: list[AgentProc] = []
        for pid in _unit_pids(unit_dir, out):
            try:
                argv0 = (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")[0].decode(errors="replace")
            except OSError:
                continue   # exited between the two reads
            name = os.path.basename(argv0)
            if name in AGENT_CLI_NAMES:
                found.append(AgentProc(pid, name, _elapsed(proc_root, pid)))
        return len(found), found
    except Exception as e:  # noqa: BLE001 - see the docstring: a broken probe must never block
        if out:
            out(f"restart guard: the busy probe failed ({type(e).__name__}: {e}); treating the gateway as idle")
        return 0, []


def gateway_busy_strict(runner: Runner = default_runner, *, cgroup_root: Path = CGROUP_ROOT,
                        proc_root: Path = PROC_ROOT) -> int | None:
    """Number of agent runs in flight, or None when the probe could not tell.

    `gateway_busy` fails OPEN (a broken probe reads as idle), which suits an upgrade guard. A
    restart-required config apply must fail CLOSED, so any note the probe emits about a failure
    turns the answer into None, and callers treat None as busy. A plainly stopped unit is not a
    failure: nothing is in flight.
    """
    problems: list[str] = []
    count, _procs = gateway_busy(runner, cgroup_root=cgroup_root, proc_root=proc_root, out=problems.append)
    return None if problems else count


def gateway_main_pid(runner: Runner = default_runner) -> str:
    """The unit's MainPID, "" when unknown. It changes on every restart, which is how a pending
    restart is recognised as done without asking the gateway anything."""
    rc, out = runner(["systemctl", "--user", "show", GATEWAY_UNIT, "-p", "MainPID", "--value"],
                     env=systemd_env())
    return out.strip() if rc == 0 else ""


def gateway_started_at(runner: Runner = default_runner) -> float | None:
    """When the gateway unit last became active, as epoch seconds; None when it cannot be told.

    `ActiveEnterTimestamp` moves on every start and restart, so it is the moment the running
    process loaded whatever plugin code was on disk then. Same unit, runner and env as the busy
    probe above, so the reads cannot drift. A stopped unit, a failed systemctl, an empty or
    unparseable answer are all None: the caller then judges by the path alone rather than
    inventing staleness from a clock it could not read.

    Asked as `--timestamp=unix`, which prints `@<epoch>`. The default form is local time with a
    zone abbreviation, and mapping that back is a guess during a DST fall-back hour: an hour early
    makes a kit installed in that window look older than the gateway, the very false negative the
    time rule exists to prevent. A systemd without the flag fails the call, and any answer that is
    not `@<digits>` is None as well: the caller then judges by the path alone, the safe direction.
    """
    try:
        rc, out = runner(["systemctl", "--user", "show", GATEWAY_UNIT, "-p", "ActiveEnterTimestamp", "--value",
                          "--timestamp=unix"], env=systemd_env())
        stamp = out.strip()
        if rc != 0 or not (stamp.startswith("@") and stamp[1:].isdigit()):
            return None
        return float(stamp[1:])
    except Exception:  # noqa: BLE001 - a broken clock read must never make the gateway look stale
        return None


def format_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    minutes = int(seconds // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m {int(seconds % 60):02d}s"


def describe_busy(workers: list[AgentProc]) -> str:
    """One line per worker, for the prompt and for the record of a deferral."""
    return "\n".join(f"  pid {w.pid}  {w.agent}  running {format_elapsed(w.elapsed)}" for w in workers)


def render_pending(pending: dict) -> str:
    """The `status` block for a gateway restart that setup deferred."""
    return ("restart     PENDING: setup deferred the gateway restart (" + str(pending.get("reason", "")) + ")\n"
            f"            since {pending.get('since', '?')}; finish with: {pending.get('command', 'openclaw gateway restart')}")


def normalize_pending(pending) -> dict:
    """A pending-restart record whose shape can be trusted, or a stand-in that keeps the gap visible.

    The record is read back from setup-state.yaml, which can be hand-edited or written by another
    kit version, and this runs in the reporting paths, exactly when the state may already be bad.
    A record that is not a mapping is NOT "nothing owed": it becomes a pending record with no
    usable pid, so it stays reported until a setup run clears it."""
    if isinstance(pending, dict):
        return pending
    return {"reason": f"unreadable pending-restart record {pending!r}", "command": "openclaw gateway restart",
            "main_pid": "", "since": "?"}


def restart_done(pending: dict, current_pid: str) -> bool:
    """Whether the gateway restarted since the deferral: both pids known and different.

    A pid that could not be read when the deferral was recorded cannot be compared with anything,
    so an empty recorded pid means "still pending", never "done": otherwise any pid read later
    would look like a restart and the gap would clear itself while the old plugin still runs."""
    recorded = str(pending.get("main_pid") or "")
    return bool(recorded) and bool(current_pid) and current_pid != recorded


def pending_restart_report(pending, runner: Runner = default_runner) -> str:
    """render_pending while the gateway has not restarted since the deferral, else "".

    Done means the MainPID differs from the one recorded. An unreadable pid, now or at record
    time, is not done: only proof clears a gap that setup named.
    """
    if pending is None or pending == {}:
        return ""
    pending = normalize_pending(pending)
    if restart_done(pending, gateway_main_pid(runner)):
        return ""
    return render_pending(pending)


# --- the canonical host configuration (profiles/openclaw-host.json5) ---------------------------------

PROFILE_PATH_NAME = Path("profiles") / "openclaw-host.json5"
# Entries whose model belongs to the engine section (the antigravity worker agent).
ENGINE_OWNED_ENTRIES = ("claude",)
# A dict with these keys is one value (a SecretRef), not a subtree to merge into.
_ATOMIC_KEYS = {"source", "id"}
_HOST_MARKERS = ("HOME", "DOMAIN", "POD_CIDR")


def load_json5(text: str) -> dict:
    """Parse the JSON5 subset the kit's profile uses: // and /* */ comments and trailing commas.

    No dependency on a JSON5 library (CI installs none). Strings are honoured, so a `//` inside
    a URL is not a comment.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    try:
        return json.loads(_drop_trailing_commas("".join(out)))
    except RecursionError:
        raise ValueError("the document is nested too deeply") from None


def _drop_trailing_commas(text: str) -> str:
    """`text` without the commas that sit right before a `]` or `}` (whitespace between is allowed).

    String-aware: a `,]` inside a quoted value is the value, not a trailing comma."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n or text[j] not in "]}":
                out.append(c)
        else:
            out.append(c)
        i += 1
    return "".join(out)


def load_host_profile(path: Path | None = None) -> dict:
    return load_json5((path or (repo_root() / PROFILE_PATH_NAME)).read_text(encoding="utf-8"))


# --- operator overrides: ~/.openclaw/kit-host-overrides.json5 (ADR-0003) ----------------------------------------
#
# The profile FILLS keys the operator has not set. A key the operator set to something else is kept
# unless the overrides file opts it back into the profile value:
#     { "keep": ["gateway.controlUi"], "force": ["gateway.bind", "tools"] }
# An entry is a dotted path (or a prefix of one); a `*` segment matches any one segment, and a lone
# "*" in `force` means every key (the 2.0.x behaviour). The file is parsed, never shell-sourced.

_OVERRIDE_PATH = re.compile(r"^[A-Za-z0-9_*-]+(\.[A-Za-z0-9_*-]+)*$")


# `force: ["*"]`: every profile key wins over the operator's value (the 2.0.x behaviour).
FORCE_ALL: dict[str, frozenset[str]] = {"keep": frozenset(), "force": frozenset({"*"})}


def host_overrides_path() -> Path:
    return Path.home() / ".openclaw" / "kit-host-overrides.json5"


def load_host_overrides(path: Path | None = None) -> dict[str, frozenset[str]]:
    """{"keep": frozenset, "force": frozenset} from the overrides file; both empty when it is absent.

    Raises ValueError naming the file and the offending PATH on bad content. A value is never
    printed: an entry that is not a string is reported by its position only.
    """
    path = path or host_overrides_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"keep": frozenset(), "force": frozenset()}
    except OSError as e:
        raise ValueError(f"{path}: cannot be read ({e.strerror or 'error'})") from None
    try:
        data = load_json5(text)
    except ValueError:
        raise ValueError(f"{path}: not valid JSON5") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected an object with `keep` and `force` lists")
    out: dict[str, frozenset[str]] = {}
    for name in ("keep", "force"):
        entries = data.get(name, [])
        if not isinstance(entries, list):
            raise ValueError(f"{path}: `{name}` must be a list of dotted paths")
        seen: set[str] = set()
        for i, entry in enumerate(entries):
            if not isinstance(entry, str) or not _OVERRIDE_PATH.match(entry):
                shown = entry if isinstance(entry, str) else f"entry #{i + 1}"
                raise ValueError(f"{path}: `{name}` has an invalid path: {shown!r}")
            if is_secret_path(entry.split(".")):
                raise ValueError(f"{path}: `{name}` names a credential path ({entry}); "
                                 "credentials are set with `openclaw configure`, never by the kit")
            seen.add(entry)
        out[name] = frozenset(seen)
    unknown = set(data) - {"keep", "force"}
    if unknown:
        raise ValueError(f"{path}: unknown key(s): {', '.join(sorted(str(k) for k in unknown))}")
    both = out["keep"] & out["force"]
    if both:
        raise ValueError(f"{path}: {sorted(both)[0]} is in both `keep` and `force`")
    return out


def _override_hit(path: list[str], entries) -> bool:
    """True when `path` is, or sits under, one of the dotted `entries` (a `*` segment matches any)."""
    for entry in entries or ():
        segs = entry.split(".")
        if len(segs) <= len(path) and all(a == "*" or a == b for a, b in zip(segs, path)):
            return True
    return False


_HOSTNAME = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")


def validate_host_values(values: dict[str, str]) -> list[str]:
    """Problems with the values the wizard collected; empty when they are usable.

    Empty domain / CIDR are allowed: the keys that need them are then simply not sent.
    """
    problems = []
    domain = values.get("DOMAIN", "")
    if domain and not _HOSTNAME.match(domain):
        problems.append(f"domain {domain!r} is not a host name (no scheme, no path)")
    cidr = values.get("POD_CIDR", "")
    if cidr:
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            problems.append(f"{cidr!r} is not a CIDR such as 10.42.0.0/24")
    owner = values.get("OWNER_TELEGRAM_ID", "")
    if owner and not re.fullmatch(r"-?\d{5,15}", owner):
        problems.append("the operator id must be the numeric Telegram user id")
    backup = values.get("BACKUP_DIR", "")
    if backup and not (backup.startswith("/") and _ENV_SAFE_VALUE.match(backup) and " " not in backup):
        problems.append("the backup path must be absolute, without spaces or shell characters")
    return problems


def _substitute(node, values: dict[str, str], missing: set[str]):
    if isinstance(node, dict):
        return {k: _substitute(v, values, missing) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, values, missing) for v in node]
    if isinstance(node, str):
        def repl(m):
            key = m.group(1)
            if not values.get(key):
                missing.add(key)
                return m.group(0)
            return values[key]
        return _MARKER.sub(repl, node)
    return node


def _has_marker(node) -> bool:
    if isinstance(node, dict):
        return any(_has_marker(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_marker(v) for v in node)
    return isinstance(node, str) and bool(_MARKER.search(node))


def _get_path(doc, path: list[str]):
    cur = doc
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None, False
        cur = cur[k]
    return cur, True


def _is_atomic(node) -> bool:
    return isinstance(node, dict) and _ATOMIC_KEYS <= set(node)


def _leaves(node, path: list[str]):
    """(path, value) for every value the patch sets: scalars, arrays and atomic dicts."""
    if isinstance(node, dict) and not _is_atomic(node):
        for k, v in node.items():
            yield from _leaves(v, path + [k])
    else:
        yield path, node


def tailnet_available() -> bool:
    """True when this host is on a tailnet: the `tailscale` CLI exists and reports an IPv4 address.

    `gateway.bind: tailnet` has no interface to bind to without it, and OpenClaw silently falls back
    to loopback, so the kit must not write that value on a host without Tailscale.
    """
    exe = shutil.which("tailscale")
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def _secret_env_available(name: str) -> bool:
    if os.environ.get(name):
        return True
    try:
        for line in (Path.home() / ".openclaw" / ".env").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{name}=") and line.split("=", 1)[1].strip():
                return True
    except OSError:
        pass
    return False


def _union(existing, wanted: list):
    base = list(existing) if isinstance(existing, list) else []
    return base + [w for w in wanted if w not in base]


def model_spec(value) -> dict:
    """The `{primary, fallbacks}` form of an agent's `model` field, whichever way it was written.

    The OpenClaw schema is `anyOf: [string, {primary, fallbacks}]`, and `openclaw agents add
    --model <id>` writes the string form, so every reader of `model` must be total: a string is
    the primary, an object is taken as-is, anything else (absent, null, a number) is empty.
    """
    if isinstance(value, str):
        return {"primary": value}
    if isinstance(value, dict):
        return value
    return {}


def model_primary(entry) -> str | None:
    """The primary model of an agent entry (or of `agents.defaults`), in either spelling."""
    primary = model_spec((entry or {}).get("model")).get("primary")
    return primary if isinstance(primary, str) and primary else None


def _is_haiku(model) -> bool:
    return isinstance(model, str) and "haiku" in model.lower()


def expand_wildcards(profile: dict, doc: dict) -> dict:
    """Replace the `*` agent entry with one concrete entry per agent that needs it."""
    tree = json.loads(json.dumps(profile))
    entries = ((tree.get("agents") or {}).get("entries")) or {}
    template = entries.pop("*", None)
    if template is not None:
        for aid, entry in ((doc.get("agents") or {}).get("entries") or {}).items():
            if aid in ENGINE_OWNED_ENTRIES:
                continue
            primary = model_primary(entry)
            if primary and not _is_haiku(primary):
                continue
            merged = json.loads(json.dumps(template))
            for k, v in (entries.get(aid) or {}).items():
                merged[k] = v
            entries[aid] = merged
    return tree


def assert_channels_safe(patch: dict, replace_paths: list[str] | None = None) -> None:
    """The one invariant this module must never break: no allowlist, no topic binding, no
    replace-path anywhere on `channels`. Only `channels.telegram.streaming.*` may be sent."""
    for rp in replace_paths or []:
        if rp == "channels" or rp.startswith("channels.") or rp.startswith("channels["):
            raise ValueError(f"refusing a --replace-path on the channels namespace: {rp}")
    channels = patch.get("channels")
    if channels is None:
        return
    allowed = isinstance(channels, dict) and set(channels) == {"telegram"} \
        and isinstance(channels["telegram"], dict) and set(channels["telegram"]) == {"streaming"}
    if not allowed:
        raise ValueError("a host patch may only touch channels.telegram.streaming")


_SECRET_KEY = re.compile(r"(token|secret|password|passwd|api[_-]?key|credential)", re.IGNORECASE)


def is_secret_path(path: list[str]) -> bool:
    """A leaf whose value can be a literal credential (a token may be pasted in as a string).
    Its previous value is never recorded: setup-state.yaml must not become a second copy."""
    return bool(path) and bool(_SECRET_KEY.search(str(path[-1])))


# The resource-guard leaves (ADR-0004). Filled like any other profile key (fill-only, hot), and skipped as a
# group when OPENCLAW_RESOURCE_GUARDS=off. The overrides `keep` list cannot express "do not fill an unset
# key" (it only protects a value that is already there), so this switch exists.
RESOURCE_GUARD_PATHS: tuple[tuple[str, ...], ...] = (("mcp", "sessionIdleTtlMs"),
                                                     ("agents", "defaults", "timeoutSeconds"))


def resource_guards_off(values: dict[str, str] | None = None, env: dict[str, str] | None = None) -> bool:
    """True when OPENCLAW_RESOURCE_GUARDS is `off`: the process environment wins over the host env file."""
    env = os.environ if env is None else env
    raw = env.get("OPENCLAW_RESOURCE_GUARDS")
    if raw is None:
        raw = (values or {}).get("RESOURCE_GUARDS", "")
    return raw.strip().lower() == "off"


def build_host_patch(profile: dict, doc: dict, values: dict[str, str], *,
                     overrides: dict | None = None) -> dict:
    """The minimal patch that FILLS the keys `doc` lacks (ADR-0003: the profile never overwrites).

    A leaf the operator set to a different value is kept, unless `overrides["force"]` names it.
    The one policy exception is an agent still on a haiku primary (runbook B2.1), which the wildcard entry
    exists to fix.

    Returns {"patch": {...}, "changes": [{"path": [...], "previous": v, "had": bool, "action":
             "filled"|"forced"}], "replace_paths": [...], "skipped": [str],
             "kept": [dotted], "filled": [dotted], "forced": [dotted]}.
    """
    overrides = overrides or {}
    keep_set, force_set = overrides.get("keep", frozenset()), overrides.get("force", frozenset())
    values = dict(values)
    values.setdefault("HOME", str(Path.home()))
    # The one declaration of the sonnet pin (model_pins), resolved at call time.
    values.setdefault("MODEL_SONNET", model_pins.effective()["sonnet"])
    tree = expand_wildcards(profile, doc)
    skipped: list[str] = []
    missing: set[str] = set()
    tree = _substitute(tree, values, missing)

    patch: dict = {}
    changes: list[dict] = []
    replace_paths: list[str] = []
    kept: list[str] = []
    guards_off = resource_guards_off(values)
    for path, wanted in _leaves(tree, []):
        dotted = ".".join(path)
        if guards_off and tuple(path) in RESOURCE_GUARD_PATHS:
            skipped.append(f"{dotted}: resource guards are off (OPENCLAW_RESOURCE_GUARDS=off)")
            continue
        if _has_marker(wanted):
            skipped.append(f"{dotted}: needs " + ", ".join(sorted(missing)) + " (not set)")
            continue
        # A plugin's settings are only written when the plugin is already configured.
        if path[:2] == ["plugins", "entries"] and len(path) > 2:
            if not _get_path(doc, path[:3])[1]:
                skipped.append(f"{dotted}: plugin {path[2]} is not configured")
                continue
        # The profile binds the gateway to the tailnet (T04). Without Tailscale that bind cannot be honoured, so
        # the value is left as the operator has it. `TAILNET` is "0" only when the caller detected no tailnet;
        # absent means "unknown", which keeps the profile's value (the golden fixtures rely on that).
        if path == ["gateway", "bind"] and wanted == "tailnet" and values.get("TAILNET") == "0":
            skipped.append(f"{dotted}: tailnet needs Tailscale, which is not running on this host (bind left as it is)")
            continue
        if path == ["gateway", "controlUi", "github", "token"] and not _secret_env_available("GH_TOKEN"):
            skipped.append(f"{dotted}: GH_TOKEN is not available on this host (set it with `openclaw configure`)")
            continue
        current, had = _get_path(doc, path)
        if isinstance(wanted, list):
            merged = _union(current, wanted)
            if had and _override_hit(path, keep_set):
                if not _strict_equal(merged, current):
                    kept.append(dotted)        # `keep` blocks the union under this path
                continue
            wanted = merged
        if had and _strict_equal(current, wanted):
            continue
        if had and isinstance(wanted, list) and isinstance(current, list):
            action = "filled"            # an add-only union: nothing the operator listed is lost
        elif not had:
            action = "filled"
        elif _override_hit(path, force_set) and not _override_hit(path, keep_set):
            action = "forced"
        elif path[:2] == ["agents", "entries"] and path[-2:] == ["model", "primary"] and _is_haiku(current):
            action = "forced"            # runbook B2.1: no orchestrator may run haiku
        else:
            kept.append(dotted)
            continue
        cur = patch
        for k in path[:-1]:
            cur = cur.setdefault(k, {})
        cur[path[-1]] = wanted
        secret = is_secret_path(path)
        # A secret leaf records that it existed and nothing of its value.
        change = {"path": path, "previous": None if secret else (current if had else None), "had": had,
                  "action": action}
        if secret:
            change["secret"] = True
        if not had:
            # Restore deletes the highest ancestor the kit created, not just the leaf, so a
            # teardown does not leave empty `model: {}` shells behind.
            change["delete"] = next(path[: i + 1] for i in range(len(path))
                                    if not _get_path(doc, path[: i + 1])[1])
        changes.append(change)
        if _is_atomic(wanted) and had and isinstance(current, dict):
            replace_paths.append(dotted)
    assert_channels_safe(patch, replace_paths)
    return {"patch": patch, "changes": changes, "replace_paths": replace_paths, "skipped": skipped,
            "kept": kept,
            "filled": [".".join(c["path"]) for c in changes if c["action"] == "filled"],
            "forced": [".".join(c["path"]) for c in changes if c["action"] == "forced"]}


def _strict_equal(a, b) -> bool:
    """Structural equality that never conflates types: True is not 1, 1 is not 1.0, "1" is not 1.

    Python's `==` says True == 1 and 1 == 1.0, which would report a real change as "nothing changed" and
    silently drop the patch. Anything unclear compares as different, so the patch is sent (fail open).
    """
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_strict_equal(x, y) for x, y in zip(a, b))
    return a == b


def change_records(doc: dict, patch: dict, replace_paths: list[str] | None, action: str) -> list[dict]:
    """Records (the shape `restore_patch` inverts) for every leaf `patch` writes into `doc`.

    Used for writers other than the host profile (the engine section) so the post-setup watch can put
    their keys back. A `--replace-path` is one record holding the whole previous subtree. A secret
    leaf records that it existed and nothing of its value.
    """
    replace = set(replace_paths or [])
    out: list[dict] = []

    def walk(node, path: list[str]) -> None:
        dotted = ".".join(path)
        if isinstance(node, dict) and node and dotted not in replace:
            for k, v in node.items():
                walk(v, path + [str(k)])
            return
        current, had = _get_path(doc, path)
        # A no-op write records nothing and puts nothing back (E1). A `null` is a delete: a key the file holds
        # (even as an explicit null) is present, so deleting it is a change and is sent.
        if (had and node is not None and _strict_equal(current, node)) or (not had and node is None):
            return
        secret = is_secret_path(path)
        ch: dict = {"path": path, "previous": None if secret else (copy.deepcopy(current) if had else None),
                    "had": had, "action": action}
        if secret:
            ch["secret"] = True
        if not had:
            ch["delete"] = next(path[: i + 1] for i in range(len(path)) if not _get_path(doc, path[: i + 1])[1])
        out.append(ch)

    walk(patch, [])
    return out


def restore_patch(changes: list[dict], doc: dict | None = None) -> tuple[dict, list[str]]:
    """The patch that puts every changed leaf back as it was (absent leaves are deleted).

    With `doc` (the live config), a leaf that already holds the restore target is skipped: a no-op
    must never open a drained window (E1).
    """
    patch: dict = {}
    replace_paths: list[str] = []
    for ch in changes:
        if ch.get("action") == "kept":
            continue                  # a kept value was never written, so there is nothing to put back
        if doc is not None and not (ch.get("secret") and ch["had"]):
            live, live_had = _get_path(doc, ch["path"])
            if (ch["had"] and live_had and _strict_equal(live, ch["previous"])) or (not ch["had"] and not live_had):
                continue
        if ch.get("secret") and ch["had"]:
            # The old value was never stored, so there is nothing to put back. Deleting the leaf
            # would destroy a credential; leave it and let the operator run `openclaw configure`.
            continue
        path = ch["path"] if ch["had"] else ch.get("delete", ch["path"])
        cur = patch
        for k in path[:-1]:
            cur = cur.setdefault(k, {})
        cur[path[-1]] = ch["previous"] if ch["had"] else None
        if ch["had"] and isinstance(ch["previous"], dict):
            replace_paths.append(".".join(path))
    assert_channels_safe(patch, replace_paths)
    return patch, replace_paths


# --- Telegram group routing: the root `bindings` array (v1.9.8) ---------------------------------------
#
# The setup writes ONLY the root `bindings` array. It never writes `channels.telegram.*`
# (`assert_channels_safe` is unchanged): when a routed group is missing from
# `channels.telegram.groups` the setup reports it, with the command for the operator to run (T35).

_GROUP_ID = re.compile(r"^-[0-9]+$")


def validate_group_id(value: str) -> str | None:
    """None when `value` has the shape of a Telegram group chat id, else why not."""
    if not _GROUP_ID.match(value.strip()):
        return "a Telegram group chat id is a minus sign and digits, e.g. -5xxxxxxxxx (a basic group)"
    return None


def is_supergroup_id(value: str) -> bool:
    """`-100...` is a supergroup/channel id; a BASIC group can never have one (T35)."""
    return value.strip().startswith("-100")


def _peer(entry) -> dict | None:
    match = entry.get("match") if isinstance(entry, dict) else None
    peer = match.get("peer") if isinstance(match, dict) else None
    return peer if isinstance(peer, dict) else None


def _group_route(entry, aid: str | None = None) -> bool:
    """A Telegram group route. Filters on `match.peer.kind`, never on `type`: the catch-all has no `type`."""
    peer = _peer(entry)
    return bool(peer and peer.get("kind") == "group"
                and entry["match"].get("channel", "telegram") == "telegram"
                and (aid is None or entry.get("agentId") == aid))


def group_binding_ids(doc: dict) -> dict[str, list[str]]:
    """agent id -> the group chat ids the live `bindings` array routes to it."""
    out: dict[str, list[str]] = {}
    bindings = doc.get("bindings")
    for entry in bindings if isinstance(bindings, list) else []:
        if _group_route(entry) and isinstance(entry.get("agentId"), str):
            out.setdefault(entry["agentId"], []).append(str(entry["match"]["peer"].get("id")))
    return out


def build_bindings(current, wanted: dict[str, str]) -> tuple[list, list[str]]:
    """The whole `bindings` array that binds each agent to its group, and what changed.

    A `config patch` replaces an array whole, so this starts from the live array and touches only what
    the kit owns, matched on `agentId` plus `match.peer.id`:
      * an agent already bound to its chat id keeps its entry byte for byte (operator `comment` too);
      * an agent bound to another group (converted to a supergroup, say) has that ONE entry's peer id
        moved in place, keeping its comment; with several group entries a new one is appended;
      * an agent with no group entry gets a new route.
    Every entry the kit does not own survives verbatim. Peer-less entries (main's catch-all: no `type`,
    no `peer`, `accountId: "*"`) are kept, in order, LAST: routing to `main` must never be shadowed.
    """
    entries = [copy.deepcopy(e) for e in current] if isinstance(current, list) else []
    notes: list[str] = []
    for aid, chat in wanted.items():
        chat = str(chat)
        own = [e for e in entries if _group_route(e, aid)]
        if any(str(e["match"]["peer"].get("id")) == chat for e in own):
            continue
        if len(own) == 1:
            notes.append(f"{aid}: group {own[0]['match']['peer'].get('id')} -> {chat}")
            own[0]["match"]["peer"]["id"] = chat
            continue
        notes.append(f"{aid}: bound to group {chat}")
        entries.append({"type": "route", "agentId": aid, "comment": f"{aid} group (ai-resources)",
                        "match": {"channel": "telegram", "peer": {"kind": "group", "id": chat}}})
    with_peer = [e for e in entries if _peer(e) is not None]
    peerless = [e for e in entries if _peer(e) is None]
    return with_peer + peerless, notes


def allowlist_gaps(doc: dict, chat_ids: list[str]) -> list[str]:
    """The chat ids Telegram would DROP without a log line: `groupPolicy: "allowlist"` and not listed."""
    telegram = ((doc.get("channels") or {}).get("telegram")) or {}
    if telegram.get("groupPolicy") != "allowlist":
        return []
    groups = telegram.get("groups")
    listed = set(groups) if isinstance(groups, dict) else set()
    return [c for c in chat_ids if c not in listed]


def allowlist_fix_command(chat_id: str) -> str:
    """The exact command that lists a group (`--merge` adds the key, it does not replace the map)."""
    value = json.dumps({chat_id: {"requireMention": False}}, separators=(",", ":"))
    return f"openclaw config set channels.telegram.groups '{value}' --strict-json --merge"


def mcp_latest_findings(doc: dict) -> list[str]:
    """`mcp.servers` entries that run an unpinned package. Reported, never rewritten."""
    out = []
    for name, spec in (((doc.get("mcp") or {}).get("servers")) or {}).items():
        args = (spec or {}).get("args") or []
        if any(isinstance(a, str) and a.endswith("@latest") for a in args):
            out.append(name)
    return sorted(out)


# --- agent workspaces: AGENTS.md templates and `agent-new` (C04) --------------------------------------

AGENTS_TEMPLATES = {
    "orchestrator": "AGENTS.orchestrator.template.md",
    "umbrella": "AGENTS.workspace-umbrella.template.md",
    "repo": "AGENTS.workspace-repo.template.md",
}
# What OpenClaw injects from a workspace, none of which belongs in the project's history.
OPENCLAW_WORKSPACE_FILES = ("AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md", "DREAMS.md",
                            "DOCS.md", "TOPIC-ROUTING.md", "memory/")
_AGENT_ID = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
TOPIC_REMINDER = ("A Telegram topic or group keeps its old context: send /new in it once so the agent "
                  "starts with this AGENTS.md (T19). A basic group is routed by an entry in the root "
                  "`bindings` array (`ai-resources setup` maintains it) and must be listed in "
                  "`channels.telegram.groups` (T35); the kit never edits channels.")


def agent_workspaces(doc: dict) -> dict[str, list[Path]]:
    """agent id -> its workspace path, resolved (`~` expanded, symlinks followed), entries order.

    Callers deduplicate on the resolved path: two agents can share one workspace, and the file
    in it is one file.
    """
    out: dict[str, list[Path]] = {}
    for aid, entry in (((doc.get("agents") or {}).get("entries")) or {}).items():
        ws = (entry or {}).get("workspace")
        if ws:
            out[aid] = [Path(ws).expanduser().resolve()]
    return out


_IDENTITY_NAME = re.compile(r"^[ \t]*[-*][ \t]*(?:\*\*)?Name:?(?:\*\*)?:?[ \t]*(.*?)[ \t]*$", re.M | re.I)


def identity_file_name(workspace: Path | str) -> str | None:
    """The `- Name:` line of a workspace's IDENTITY.md, or None. Read-only: the kit never writes it."""
    try:
        text = (Path(workspace) / "IDENTITY.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _IDENTITY_NAME.search(text)
    # An unfilled template line (`- **Name:**` with nothing after it) is no name at all.
    return (m.group(1).strip().strip("*_ \t") or None) if m else None


def agent_identities(doc: dict) -> list[dict]:
    """Per agent: the configured `identity.name`, the resolved workspace, and the name IDENTITY.md holds there.

    OpenClaw falls back to the workspace file when `identity.name` is absent, so two agents sharing a
    workspace can be narrated with the name of whichever wrote the file last.
    """
    entries = ((doc.get("agents") or {}).get("entries")) or {}
    out = []
    for aid, paths in agent_workspaces(doc).items():
        for ws in paths:
            ident = (entries.get(aid) or {}).get("identity")
            name = ident.get("name") if isinstance(ident, dict) else None
            out.append({"agent": aid, "name": name if isinstance(name, str) and name else None,
                        "workspace": str(ws), "file_name": identity_file_name(ws)})
    return out


def detect_workspace_kind(workspace: Path | str, runner: Runner = default_runner) -> str:
    """`repo` for a checkout with tracked files and a remote; `umbrella` for everything else.

    An umbrella is a directory that groups repositories: it is not a repository, or it is one
    with no commits and no remote. Anything ambiguous is an umbrella, because the umbrella
    template only warns harder against committing in the wrong place.
    """
    ws = str(workspace)
    rc, top = runner(["git", "-C", ws, "rev-parse", "--show-toplevel"])
    if rc != 0 or not top.strip():
        return "umbrella"
    rc, files = runner(["git", "-C", ws, "ls-files"])
    tracked = len([ln for ln in files.splitlines() if ln.strip()]) if rc == 0 else 0
    rc, remotes = runner(["git", "-C", ws, "remote"])
    has_remote = rc == 0 and bool(remotes.strip())
    return "repo" if tracked > 0 and has_remote else "umbrella"


def render_agents_md(kind: str, agent_id: str, agent_name: str | None = None) -> str:
    text = (repo_root() / "templates" / AGENTS_TEMPLATES[kind]).read_text(encoding="utf-8")
    return _MARKER.sub(lambda m: {"AGENT_ID": agent_id, "AGENT_NAME": agent_name or agent_id}
                       .get(m.group(1), m.group(0)), text)


def add_to_git_exclude(workspace: Path, names: tuple[str, ...] = OPENCLAW_WORKSPACE_FILES) -> bool:
    """Append the OpenClaw files to .git/info/exclude, once. False when there is nothing to do."""
    git_dir = workspace / ".git"
    if not git_dir.is_dir():
        return False
    exclude = git_dir / "info" / "exclude"
    try:
        current = exclude.read_text(encoding="utf-8")
    except OSError:
        current = ""
    have = {ln.strip() for ln in current.splitlines()}
    missing = [n for n in names if n not in have]
    if not missing:
        return False
    exclude.parent.mkdir(parents=True, exist_ok=True)
    block = "" if not current or current.endswith("\n") else "\n"
    if "# OpenClaw workspace files (ai-resources)" not in current:
        block += "# OpenClaw workspace files (ai-resources)\n"
    exclude.write_text(current + block + "\n".join(missing) + "\n", encoding="utf-8")
    return True


def write_agents_md(workspace: Path, kind: str, agent_id: str, *, force: bool = False,
                    agent_name: str | None = None) -> str:
    """Returns `written`, `overwritten` or `kept` (an existing file is never replaced without force)."""
    target = workspace / "AGENTS.md"
    existed = target.exists()
    if existed and not force:
        return "kept"
    workspace.mkdir(parents=True, exist_ok=True)
    target.write_text(render_agents_md(kind, agent_id, agent_name), encoding="utf-8")
    return "overwritten" if existed else "written"


def agent_new(agent_id: str, workspace: Path | str, *, kind: str | None = None, force: bool = False,
              model: str = "", register: bool = True, runner: Runner = default_runner,
              openclaw_bin: str | None = None) -> dict:
    """Give an OpenClaw agent a workspace with the right AGENTS.md.

    Registers the agent through OpenClaw's own `agents add` (which also creates its agentDir and
    session store, and is a validated writer), never through openclaw.json. Refuses to overwrite an
    existing AGENTS.md without `force`. The Telegram topic binding is not done here: it lives in
    `channels`, which the kit never edits.
    """
    if not _AGENT_ID.match(agent_id):
        raise ValueError("the agent id must be lowercase letters, digits and dashes, starting with a letter")
    ws = Path(workspace).expanduser().resolve()
    if not ws.is_dir():
        raise FileNotFoundError(f"workspace does not exist: {ws}")
    kind = kind or detect_workspace_kind(ws, runner)
    if kind not in AGENTS_TEMPLATES:
        raise ValueError(f"unknown workspace kind {kind!r}")
    result: dict = {"kind": kind, "workspace": str(ws), "agents_md": write_agents_md(ws, kind, agent_id, force=force)}
    result["excluded"] = add_to_git_exclude(ws)
    result["registered"] = None
    if register:
        oc = openclaw_bin or resolve_openclaw_bin()
        rc, listing = runner([oc, "agents", "list", "--json"])
        if rc == 0 and re.search(rf'"id"\s*:\s*"{re.escape(agent_id)}"', listing):
            result["registered"] = "existing"
        else:
            args = [oc, "agents", "add", agent_id, "--workspace", str(ws), "--non-interactive"]
            if model:
                args += ["--model", model]
            rc, out = runner(args)
            result["registered"] = "added" if rc == 0 else f"failed: {out[-200:]}"
    result["reminder"] = TOPIC_REMINDER
    return result


# --- bootstrap: bring a host to the documented state, idempotently (C01) -----------------------------

ALLOW_SCRIPTS = "openclaw,@google/genai,koffi,tree-sitter-bash,protobufjs"
BOOTSTRAP_STEPS = ("node", "openclaw", "single-copy", "gateway-unit", "boot", "backup-dirs",
                   "memory-high", "units")
BOOTSTRAP_TITLES = {
    "node": "node satisfies openclaw's engines range (>=24.16 <25 || >=26.1)",
    "openclaw": "openclaw is installed with its install scripts allowed (T06)",
    "single-copy": "exactly one global openclaw",
    "gateway-unit": "the gateway unit runs brew's node by absolute path (T07)",
    "boot": "the gateway starts at boot (linger + enabled)",
    "backup-dirs": "the backup tier directories exist and are yours",
    "memory-high": "MemoryHigh is set with set-property, never a drop-in (T27)",
    "units": "the openclaw-* units are installed and their timers enabled",
}
_EPHEMERAL_PATH = ("multishell", "/run/user/")
# Where a version manager keeps its own node (and so its own global openclaw), relative to $HOME.
_VERSION_MANAGER_GLOBS = (".nvm/versions/node/*/bin", ".local/share/fnm/node-versions/*/installation/bin",
                          ".fnm/node-versions/*/installation/bin", ".volta/bin",
                          ".asdf/installs/nodejs/*/bin", ".local/share/mise/installs/node/*/bin")


def node_version_ok(version: str) -> bool:
    """`>=24.16 <25 || >=26.1`: non-contiguous, so trusting whatever node brew has is not enough."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version.strip())
    if not m:
        return False
    major, minor = int(m.group(1)), int(m.group(2))
    return (major == 24 and minor >= 16) or (major == 26 and minor >= 1) or major > 26


def _bytes(size: str) -> int:
    m = re.fullmatch(r"(\d+)([KMGT]?)", size.strip().upper())
    if not m:
        raise ValueError(f"not a size such as 12G: {size!r}")
    return int(m.group(1)) * 1024 ** "_KMGT".index(m.group(2) or "_")


def _package_root(binary: Path) -> Path | None:
    """`.../node_modules/openclaw` for an installed openclaw executable (or its entry script)."""
    for parent in [binary.resolve(), *binary.resolve().parents]:
        if parent.name == "openclaw" and parent.parent.name == "node_modules":
            return parent
    return None


def _execstart_argv(text: str) -> list[str]:
    m = re.search(r"argv\[\]=(.*?)\s;", text)
    return m.group(1).split() if m else []


class StepResult:
    def __init__(self, step: str, status: str, detail: str = ""):
        self.step, self.status, self.detail = step, status, detail

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"StepResult({self.step}, {self.status}, {self.detail!r})"


OK_STATUSES = {"satisfied", "changed", "would-change", "skipped"}


class _Ctx:
    """What every step shares: the runner, the dry-run switch and the record of what it ran."""

    def __init__(self, runner, *, dry_run, confirm, home, upgrade, memory_high, backup_dir,
                 host_env_path, sleep, health_wait, node_bin, env, unit_dir=None):
        self.runner, self.dry_run, self.confirm, self.home = runner, dry_run, confirm, home
        self.upgrade, self.memory_high, self.backup_dir = upgrade, memory_high, backup_dir
        self.host_env_path, self.sleep, self.health_wait = host_env_path, sleep, health_wait
        self.node_bin, self.env, self.unit_dir = node_bin, env, unit_dir
        self.commands: list[list[str]] = []   # every command actually run (never in dry-run)
        self.planned: list[str] = []          # what a dry run says it would do

    def run(self, argv: list[str], **kw) -> tuple[int, str]:
        """A read-only probe. Always runs, in dry-run too."""
        return self.runner(argv, env=self.env, **kw)

    def mutate(self, step: str, argv: list[str], why: str, **kw) -> tuple[int, str] | None:
        """A change. In dry-run only recorded; otherwise confirmed, recorded and run.
        Returns None when nothing was run (dry-run or declined)."""
        shown = " ".join(argv)
        if self.dry_run:
            self.planned.append(f"{step}: {shown}   ({why})")
            return None
        if not self.confirm(step, f"{why}: {shown}"):
            return None
        self.commands.append(list(argv))
        return self.runner(argv, env=self.env, **kw)


def _node_bin(runner, env) -> str:
    rc, prefix = runner(["brew", "--prefix", "node"], env=env)
    if rc == 0 and prefix.strip() and (Path(prefix.strip()) / "bin" / "node").exists():
        return str(Path(prefix.strip()) / "bin" / "node")
    return shutil.which("node") or ""


def _step_node(c: _Ctx) -> StepResult:
    rc, out = c.run([c.node_bin or "node", "--version"])
    if rc == 0 and node_version_ok(out):
        return StepResult("node", "satisfied", out.strip())
    if rc != 0:
        out = "node is not installed"
    if c.run(["brew", "--version"])[0] != 0:
        return StepResult("node", "refused", f"{out}; Homebrew is missing, install node >=24.16 by hand")
    r = c.mutate("node", ["brew", "install", "node"], "install a node that satisfies the range", timeout=900)
    if r is None:
        return StepResult("node", "would-change" if c.dry_run else "declined", out.strip())
    c.node_bin = _node_bin(c.runner, c.env)
    rc, ver = c.run([c.node_bin or "node", "--version"])
    ok = rc == 0 and node_version_ok(ver)
    return StepResult("node", "changed" if ok else "failed", ver.strip() or out.strip())


def _native_ok(c: _Ctx) -> tuple[bool, str]:
    rc, root = c.run(["npm", "root", "-g"])
    if rc != 0 or not root.strip():
        return False, "cannot locate the global node_modules (npm root -g)"
    pkg = Path(root.strip().splitlines()[-1]) / "openclaw" / "node_modules"
    tree = list(pkg.glob("tree-sitter-bash/**/*.node"))
    koffi = list(pkg.glob("@koromix/koffi-*/**/*.node"))
    if not tree or not koffi:
        missing = [n for n, found in (("tree-sitter-bash", tree), ("koffi", koffi)) if not found]
        return False, "native modules missing: " + ", ".join(missing)
    return True, ""


def _openclaw_version(c: _Ctx) -> str:
    rc, out = c.run(["openclaw", "--version"])
    m = re.search(r"\d{4}\.\d+\.\d+", out) if rc == 0 else None
    return m.group(0) if m else ""


def _gateway_active(c: _Ctx) -> bool:
    rc, out = c.run(["systemctl", "--user", "is-active", GATEWAY_UNIT])
    return rc == 0 and out.strip() == "active"


def _step_openclaw(c: _Ctx) -> StepResult:
    current = _openclaw_version(c)
    native_ok, why = _native_ok(c) if current else (False, "")
    if current and native_ok and not c.upgrade:
        return StepResult("openclaw", "satisfied", current)
    reason = ("not installed" if not current else why if not native_ok else "upgrade requested")
    npm = str(Path(c.node_bin).with_name("npm")) if c.node_bin and Path(c.node_bin).with_name("npm").exists() else "npm"
    argv = [npm, "install", "-g", f"--allow-scripts={ALLOW_SCRIPTS}", "openclaw@latest"]
    if c.mutate("openclaw", argv, f"install the latest openclaw ({reason})", timeout=1200) is None:
        return StepResult("openclaw", "would-change" if c.dry_run else "declined", reason)
    new = _openclaw_version(c)
    native_ok, why = _native_ok(c)
    if not new or not native_ok:
        return StepResult("openclaw", "failed", why or "openclaw does not report a version after the install")
    # Always latest, so the one thing that makes it reversible is recording what was there.
    values = {"OPENCLAW_INSTALLED_VERSION": new}
    if current:
        values["OPENCLAW_PREVIOUS_VERSION"] = current
    write_host_env(values, c.host_env_path)
    rollback = (f"npm install -g --allow-scripts={ALLOW_SCRIPTS} openclaw@{current}" if current else "")
    if _gateway_active(c):
        healthy = False
        for _ in range(max(1, int(c.health_wait // 5))):
            rc, _out = c.run(["openclaw", "health"])
            if rc == 0:
                healthy = True
                break
            c.sleep(5)
        if not healthy:
            hint = f" Roll back with: {rollback}" if rollback else ""
            return StepResult("openclaw", "failed", f"{new} installed but the gateway does not answer.{hint}")
    return StepResult("openclaw", "changed", f"{current or 'none'} -> {new}"
                      + (f" (roll back: {rollback})" if rollback else ""))


def find_openclaw_copies(home: Path, path_env: str) -> list[Path]:
    """Every distinct global openclaw executable: on PATH and in the usual version-manager homes."""
    seen: dict[str, Path] = {}
    candidates = [Path(d) / "openclaw" for d in path_env.split(os.pathsep) if d]
    for pattern in _VERSION_MANAGER_GLOBS:
        candidates += [d / "openclaw" for d in sorted(home.glob(pattern))]
    for cand in candidates:
        if cand.exists():
            seen.setdefault(str(cand.resolve()), cand)
    return list(seen.values())


def _is_version_managed(copy: Path, home: Path) -> bool:
    return any(copy.parent == d for pattern in _VERSION_MANAGER_GLOBS for d in home.glob(pattern))


def _step_single_copy(c: _Ctx) -> StepResult:
    copies = find_openclaw_copies(c.home, c.env.get("PATH", os.environ.get("PATH", "")))
    roots = {str(_package_root(p)): p for p in copies if _package_root(p)}
    if len(roots) <= 1:
        return StepResult("single-copy", "satisfied", f"{len(roots)} copy")
    rc, out = c.run(["systemctl", "--user", "show", GATEWAY_UNIT, "-p", "ExecStart", "--value"])
    running = None
    for token in (_execstart_argv(out) if rc == 0 else []):
        m = re.match(r"^(.*/node_modules/openclaw)/", token)
        if m:
            running = Path(m.group(1)).resolve()
            break
    known = {Path(r).resolve() for r in roots}
    if running is None or running not in known:
        return StepResult("single-copy", "refused",
                          f"{len(roots)} copies found but the running unit's ExecStart does not prove "
                          "which one it uses; removing blindly could break the gateway")
    shadowed = [p for r, p in roots.items() if Path(r).resolve() != running
                and _is_version_managed(p, c.home)]
    if not shadowed:
        return StepResult("single-copy", "refused",
                          "several copies, but none of the extra ones lives under a version manager")
    detail = []
    for copy in shadowed:
        npm = copy.parent / "npm"
        if not npm.exists():
            return StepResult("single-copy", "refused", f"no npm next to {copy}; remove it by hand")
        r = c.mutate("single-copy", [str(npm), "uninstall", "-g", "openclaw"],
                     f"remove the shadowed copy at {copy}")
        detail.append(str(copy))
        if r is None and not c.dry_run:
            return StepResult("single-copy", "declined", str(copy))
    if c.dry_run:
        return StepResult("single-copy", "would-change", "would remove: " + ", ".join(detail))
    return StepResult("single-copy", "changed", "removed: " + ", ".join(detail))


def _unit_facts(c: _Ctx) -> tuple[str, str]:
    rc, out = c.run(["systemctl", "--user", "cat", GATEWAY_UNIT])
    return (out if rc == 0 else ""), ("" if rc == 0 else out)


def _gateway_unit_problem(c: _Ctx) -> str:
    text, _err = _unit_facts(c)
    if not text:
        return "no gateway unit installed"
    exec_line = next((ln for ln in text.splitlines() if ln.startswith("ExecStart=")), "")
    env_path = next((ln for ln in text.splitlines() if ln.startswith("Environment=") and "PATH=" in ln), "")
    for line in (exec_line, env_path):
        if any(bad in line for bad in _EPHEMERAL_PATH):
            return "the unit references an ephemeral path (T07)"
    first = exec_line.split("=", 1)[1].split()[0] if "=" in exec_line and exec_line.split("=", 1)[1].split() else ""
    want = c.node_bin
    if not want or not first.startswith("/"):
        return "ExecStart does not start with an absolute node path (T07)"
    if os.path.realpath(first) != os.path.realpath(want):
        return f"ExecStart runs {first}, not brew's node {want} (T07)"
    return ""


def _step_gateway_unit(c: _Ctx) -> StepResult:
    problem = _gateway_unit_problem(c)
    if not problem:
        return StepResult("gateway-unit", "satisfied", "")
    oc = shutil.which("openclaw") or "openclaw"
    entry = os.path.realpath(oc) if os.path.exists(oc) else oc
    argv = [c.node_bin or "node", entry, "gateway", "install", "--force"]
    if c.mutate("gateway-unit", argv, f"regenerate the gateway unit ({problem}); the kit does not restart it") is None:
        return StepResult("gateway-unit", "would-change" if c.dry_run else "declined", problem)
    after = _gateway_unit_problem(c)
    if after:
        return StepResult("gateway-unit", "failed", f"{after}; fix the unit and re-run (T07)")
    return StepResult("gateway-unit", "changed", "unit regenerated; a restart is needed to pick it up")


def _step_boot(c: _Ctx) -> StepResult:
    user = c.env.get("USER") or os.environ.get("USER", "")
    rc, linger = c.run(["loginctl", "show-user", user, "-p", "Linger", "--value"])
    rc2, enabled = c.run(["systemctl", "--user", "is-enabled", GATEWAY_UNIT])
    need_linger = not (rc == 0 and linger.strip() == "yes")
    need_enable = not (rc2 == 0 and enabled.strip() == "enabled")
    if not need_linger and not need_enable:
        return StepResult("boot", "satisfied", "linger + enabled")
    ran = False
    for needed, argv, why in ((need_linger, ["loginctl", "enable-linger", user], "let user services run without a login"),
                              (need_enable, ["systemctl", "--user", "enable", GATEWAY_UNIT], "start the gateway at boot")):
        if needed:
            r = c.mutate("boot", argv, why)
            ran = ran or r is not None
            if r is not None and r[0] != 0:
                return StepResult("boot", "failed", r[1][-200:])
    if c.dry_run:
        return StepResult("boot", "would-change")
    return StepResult("boot", "changed" if ran else "declined")


def _step_backup_dirs(c: _Ctx) -> StepResult:
    base = Path(c.backup_dir)
    tiers = [base / t for t in ("daily", "weekly", "monthly")]
    uid = os.getuid()
    missing = [t for t in tiers if not t.is_dir() or t.stat().st_uid != uid]
    if not missing:
        return StepResult("backup-dirs", "satisfied", str(base))
    user = c.env.get("USER") or os.environ.get("USER", "")
    argv = ["sudo", "install", "-d", "-o", user, "-m", "0755", *[str(t) for t in tiers]]
    r = c.mutate("backup-dirs", argv, "create the backup tiers owned by you")
    if r is None:
        return StepResult("backup-dirs", "would-change" if c.dry_run else "declined", ", ".join(map(str, missing)))
    return StepResult("backup-dirs", "changed" if r[0] == 0 else "failed", r[1][-200:])


def _step_memory_high(c: _Ctx) -> StepResult:
    want = _bytes(c.memory_high)
    rc, cur = c.run(["systemctl", "--user", "show", GATEWAY_UNIT, "-p", "MemoryHigh", "--value"])
    if rc != 0:
        return StepResult("memory-high", "refused", "the gateway unit is not installed yet")
    if cur.strip() == str(want):
        return StepResult("memory-high", "satisfied", c.memory_high)
    argv = ["systemctl", "--user", "set-property", GATEWAY_UNIT, f"MemoryHigh={c.memory_high}"]
    r = c.mutate("memory-high", argv, "cap the gateway's memory without a drop-in")
    if r is None:
        return StepResult("memory-high", "would-change" if c.dry_run else "declined", f"currently {cur.strip() or 'unset'}")
    return StepResult("memory-high", "changed" if r[0] == 0 else "failed", r[1][-200:])


def units_state(dest: Path | None = None, runner: Runner = default_runner,
                home: Path | None = None) -> tuple[list[str], list[str]]:
    """(unit files that differ from their template, timers that are not enabled). Read-only."""
    plan = install_units(dest, dry_run=True, runner=runner, home=home)
    disabled = []
    for timer in TIMER_NAMES:
        rc, out = runner(["systemctl", "--user", "is-enabled", timer], env=systemd_env())
        if not (rc == 0 and out.strip() == "enabled"):
            disabled.append(timer)
    return plan["changed"], disabled


def _step_units(c: _Ctx) -> StepResult:
    changed, disabled = units_state(c.unit_dir, c.runner, c.home)
    plan = {"changed": changed}
    if not plan["changed"] and not disabled:
        return StepResult("units", "satisfied", f"{len(UNIT_NAMES)} units, {len(TIMER_NAMES)} timers")
    if c.dry_run:
        c.planned.append(f"units: install {len(plan['changed'])} unit(s), enable {len(disabled) or len(TIMER_NAMES)} timer(s)")
        return StepResult("units", "would-change", f"{len(plan['changed'])} to write, {len(disabled)} timers off")
    if not c.confirm("units", "install the openclaw-* units and enable their timers"):
        return StepResult("units", "declined")
    result = install_units(c.unit_dir, enable=True, runner=c.runner, home=c.home)
    c.commands.append(["ai-resources", "openclaw", "install-units", "--enable"])
    # A hand-installed health-restart timer is not the bootstrap's to take over (setup asks first), so its
    # timer is not enabled here.
    ok = len(result["enabled"]) == len(TIMER_NAMES) - (len(HEALTH_UNITS) // 2 if result["hand_copy"] else 0)
    return StepResult("units", "changed" if ok else "failed", f"wrote {len(result['changed'])}, enabled {len(result['enabled'])}")


_STEP_FUNCS = {"node": _step_node, "openclaw": _step_openclaw, "single-copy": _step_single_copy,
               "gateway-unit": _step_gateway_unit, "boot": _step_boot, "backup-dirs": _step_backup_dirs,
               "memory-high": _step_memory_high, "units": _step_units}


def bootstrap(runner: Runner = default_runner, *, dry_run: bool = False, only: str | None = None,
              upgrade: bool = False, memory_high: str = "12G", backup_dir: str | None = None,
              confirm: Callable[[str, str], bool] = lambda step, what: False,
              home: Path | None = None, host_env_path: Path | None = None,
              sleep: Callable[[float], None] = time.sleep, health_wait: float = 60,
              unit_dir: Path | None = None, skip: tuple[str, ...] = (),
              out: Callable[[str], None] = print) -> tuple[list[StepResult], _Ctx]:
    """Eight idempotent steps in a fixed order; each checks first and reports satisfied or changed.

    `dry_run` runs the checks and prints what it WOULD do; nothing is touched. `only` runs one
    step. `confirm(step, description)` is asked before every change; the default is No (the wizard passes a
    real prompt, the headless command passes yes only with `--yes`). Never writes openclaw.json, never restarts the
    gateway; the steps that change the gateway's unit or install a new version say a restart
    is needed and stop.
    """
    if only is not None and only not in BOOTSTRAP_STEPS:
        raise ValueError(f"unknown step {only!r}; choose from {', '.join(BOOTSTRAP_STEPS)}")
    env = systemd_env()
    env.setdefault("USER", os.environ.get("USER", ""))
    env_file = read_host_env(host_env_path)
    ctx = _Ctx(runner, dry_run=dry_run, confirm=confirm, home=home or Path.home(), upgrade=upgrade,
               memory_high=memory_high,
               backup_dir=backup_dir or env_file.get("OPENCLAW_BACKUP_DIR", "/srv/openclaw-backups"),
               host_env_path=host_env_path, sleep=sleep, health_wait=health_wait,
               node_bin=_node_bin(runner, env), env=env, unit_dir=unit_dir)
    results: list[StepResult] = []
    for step in BOOTSTRAP_STEPS:
        if (only is not None and step != only) or step in skip:
            continue
        try:
            res = _STEP_FUNCS[step](ctx)
        except Exception as e:  # noqa: BLE001 - a step must never take the report down with it
            res = StepResult(step, "failed", f"{type(e).__name__}: {e}")
        results.append(res)
        out(f"[{res.status:>12}] {step}: {BOOTSTRAP_TITLES[step]}" + (f"  -- {res.detail}" if res.detail else ""))
        # A later step cannot succeed on top of a failed or refused earlier one that it depends on.
        if res.status == "failed" and step in ("node", "openclaw"):
            break
    if dry_run:
        for line in ctx.planned:
            out(f"  would run  {line}")
        changing = [r for r in results if r.status == "would-change"]
        out(f"dry run: {len(changing)} step(s) would change something" if changing
            else "dry run: nothing to change")
    return results, ctx


# --- status: one screen, never repairs (C09) -----------------------------------------------------------

# T30: what `openclaw doctor` still prints on a clean host. Anything else is signal.
DOCTOR_NOISE = (
    ("heap", re.compile(r"runtime V8 ceiling: not measured", re.I)),
    ("shell-path", re.compile(r"PATH missing required dirs|Gateway service PATH includes version managers", re.I)),
    ("owned-unit", re.compile(r"Run [`\"']openclaw gateway install --force[`\"'] when you want to replace", re.I)),
    ("desktop", re.compile(r"Host desktop disabled", re.I)),
    ("legacy-bindings", re.compile(r"Legacy session bindings|Affected sessions: \d+|migrate legacy bindings and stale", re.I)),
    ("whisper", re.compile(r"whisper-cli backend cannot be proven without loading a model", re.I)),
    ("privacy-mode", re.compile(r"telegram.*privacy mode", re.I)),
    # `claude-kit` is a CLI backend, not a catalogue provider, so it can never answer a
    # model-list probe. Anchored to the provider prefix: `Unknown model: anthropic/...` is signal.
    ("claude-kit-model", re.compile(r"Unknown model: claude-kit/", re.I)),
    ("dashboard-conflict", re.compile(r'Plugin command "/dashboard" conflicts with an existing Telegram command', re.I)),
)
# The drained `doctor --fix` and the status probe share one budget so they can never drift.
# Measured on bithome 2026-09-28: `openclaw doctor --non-interactive` = rc 0 in ~29s, so 600 is
# insurance, not the fix for "could not run" (that was the unresolved binary, rc 127).
DOCTOR_TIMEOUT = 600
TIER_MAX_AGE_HOURS = {"daily": 36, "weekly": 8 * 24, "monthly": 35 * 24}
STATUS_SECTIONS = ("unit", "boot", "openclaw", "listeners", "health", "timers", "backups", "off-box", "models", "identity", "stability", "doctor")


def parse_doctor_entries(text: str) -> list[tuple[str, str]]:
    """(panel title, entry) for every bullet or paragraph in `openclaw doctor`'s boxed report,
    plus its standalone `[warning]` lines. Wrapped lines are joined into one entry."""
    entries: list[tuple[str, str]] = []
    title = ""
    current: list[str] = []

    def flush():
        if current:
            entries.append((title, " ".join(current).strip()))
            current.clear()

    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"^[\u25c7\u25c6]\s+(.*?)\s*[\u2500]+", line)
        if m:
            flush()
            title = m.group(1)
            continue
        if line.startswith(("\u251c", "\u2570")):
            flush()
            title = ""
            continue
        if re.match(r"^\[warning\]", line, re.I):
            flush()
            entries.append(("", line))
            continue
        if not title:
            continue
        body = line.strip("\u2502").strip()
        if not body:
            flush()
        elif body.startswith("- "):
            flush()
            current.append(body[2:])
        elif body.lower().startswith("fix:") or not current and not body.startswith("-"):
            flush()
            current.append(body)
        else:
            current.append(body)
    flush()
    return entries


def filter_doctor_warnings(text: str) -> tuple[list[str], list[str]]:
    """(known noise, signal): entries in a warnings panel or a `[warning]` line that the T30
    catalogue does not explain are signal; catalogued entries anywhere are noise."""
    noise: list[str] = []
    signal: list[str] = []
    prev_title, prev_noise = None, False
    for title, entry in parse_doctor_entries(text):
        if entry.lower().startswith("fix:") and title == prev_title and prev_noise:
            continue   # the remedy line of an entry already explained as noise
        prev_title = title
        prev_noise = any(pat.search(entry) for _, pat in DOCTOR_NOISE)
        if prev_noise:
            noise.append(entry)
        elif not title or "warning" in title.lower():
            signal.append(entry)
    return noise, signal


def _humanize(seconds: float) -> str:
    hours = seconds / 3600
    return f"{hours:.0f}h" if hours < 48 else f"{hours / 24:.0f}d"


def _size(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n}"


def backup_tiers(base: Path, now: float | None = None) -> dict[str, dict]:
    now = now if now is not None else time.time()
    out: dict[str, dict] = {}
    for tier in ("daily", "weekly", "monthly"):
        files = sorted((base / tier).glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True) \
            if (base / tier).is_dir() else []
        if not files:
            out[tier] = {"present": False}
            continue
        newest = files[0]
        age = now - newest.stat().st_mtime
        out[tier] = {"present": True, "name": newest.name, "age_hours": age / 3600,
                     "size": newest.stat().st_size, "count": len(files),
                     "checksum": newest.with_name(newest.name + ".sha256").exists(),
                     "stale": age / 3600 > TIER_MAX_AGE_HOURS[tier]}
    return out


# T39: the gateway writes a stability bundle when a stop or restart goes wrong. Real ones are about
# 1.5 MB; anything far above that is not read.
STABILITY_MAX_BYTES = 20 * 1024 * 1024


def _bundle_time(name: str, data: dict) -> float | None:
    """Epoch seconds from the bundle's generatedAt, else from the timestamp in its file name."""
    import datetime as dt
    raw = data.get("generatedAt") if isinstance(data, dict) else None
    if not isinstance(raw, str):
        m = re.match(r"openclaw-stability-(\d{4}-\d\d-\d\d)T(\d\d)-(\d\d)-(\d\d)", name)
        raw = f"{m[1]}T{m[2]}:{m[3]}:{m[4]}Z" if m else ""
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def stability_summary(log_dir: Path, now: float | None = None) -> dict:
    """What the newest gateway stability bundle says about stalled sessions. Read-only; never
    returns event payloads or session text. Counts are lower bounds: the ring buffer drops events."""
    now = now if now is not None else time.time()
    try:
        names = sorted(n for n in os.listdir(log_dir) if n.startswith("openclaw-stability-") and n.endswith(".json"))
    except OSError:
        return {"present": False}
    if not names:
        return {"present": False}
    name = names[-1]          # the timestamp in the name sorts as text; mtime can be reset by a copy
    out: dict = {"present": True, "name": name}
    path = Path(log_dir) / name
    try:
        if path.stat().st_size > STABILITY_MAX_BYTES:
            return {**out, "error": "bundle too large to read"}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            return {**out, "error": "unexpected bundle version"}
        snap = data.get("snapshot") or {}
        summary = snap.get("summary") or {} if isinstance(snap, dict) else None
        by_type = summary.get("byType") or {} if isinstance(summary, dict) else None
        raw_events = snap.get("events") or [] if isinstance(snap, dict) else None
        if not isinstance(by_type, dict) or not isinstance(raw_events, list):
            return {**out, "error": "unexpected bundle shape"}
        events = [e for e in raw_events if isinstance(e, dict)]
    except (OSError, ValueError) as e:
        return {**out, "error": f"unreadable: {type(e).__name__}"}
    try:
        return _stability_fields(out, name, data, snap, events, by_type, now)
    except (TypeError, ValueError, OverflowError, AttributeError):
        return {"present": True, "name": name, "error": "unexpected bundle shape"}


def _stability_fields(out: dict, name: str, data: dict, snap: dict, events: list, by_type: dict,
                      now: float) -> dict:
    stalled: dict[str, int] = {}
    tools: dict[str, int] = {}
    ages: list[float] = []
    long_running = 0
    for e in events:
        if e.get("type") == "session.stalled":
            reason = str(e.get("reason") or "unknown")
            stalled[reason] = stalled.get(reason, 0) + 1
            if isinstance(e.get("ageMs"), (int, float)):
                ages.append(e["ageMs"] / 1000)
            if reason == "blocked_tool_call" and e.get("toolName"):
                tools[str(e["toolName"])] = tools.get(str(e["toolName"]), 0) + 1
        elif e.get("type") == "session.long_running":
            long_running += 1
    if not events and by_type.get("session.stalled"):
        stalled = {"unknown": int(by_type["session.stalled"])}
    if not events:
        long_running = int(by_type.get("session.long_running") or 0)
    ts = _bundle_time(name, data)
    out.update(reason=str(data.get("reason") or "?"), generated_at=ts,
               age_hours=(now - ts) / 3600 if ts is not None else None,
               stalled=stalled, tools=tools, long_running=long_running,
               max_stalled_age_s=max(ages) if ages else 0, dropped=int(snap.get("dropped") or 0))
    return out


def recorded_selection():
    """The selection in setup-state.yaml, or None (a broken or missing state never stops a status)."""
    try:
        from .setup import state as setup_state
        return setup_state.load().get_selection()
    except Exception:  # noqa: BLE001
        return None


def is_leftover_watch_restore(entry: dict) -> bool:
    """A `source=watch` restore that was not corroborated: what the 2.1.0 watch queued on a probe that could not
    run (D1). A 2.1.1 entry carries `corroborated: True` and is a real pending revert."""
    return entry.get("source") == "watch" and entry.get("op") == "restore" and not entry.get("corroborated")


def pending_summary(o) -> dict:
    """What status and verify show about the restart-required config setup did not apply:
    {"paths": [...], "leftover_watch": [...], "watch": {...}|None}, {} when there is nothing to say.
    Paths only, never values. `leftover_watch` is kept apart from `paths`: applying it is not what the
    operator wants (see `is_leftover_watch_restore`)."""
    entries = list(getattr(o, "host_restart_pending", None) or [])
    paths = sorted({str(e.get("path", "")) for e in entries if e.get("path") and not is_leftover_watch_restore(e)})
    leftover = sorted({str(e.get("path", "")) for e in entries if e.get("path") and is_leftover_watch_restore(e)})
    cw = getattr(o, "config_watch", None) or {}
    watch = None
    unrecorded = None
    try:
        from .setup import state as setup_state
        unrecorded = setup_state.read_watch_marker()
    except Exception:  # noqa: BLE001 - a status never fails on its own bookkeeping
        unrecorded = None
    # The marker is bound to the run it describes (2.2d, 2b): it is reported only while the state's own
    # `config_watch` is that run and has no result recorded. A marker of another run, one without a run id, or one
    # for a run whose result IS recorded says nothing, so a forged or stale file never prints a failure that did
    # not happen, and it never replaces a failure the record holds (`watch`, below).
    if unrecorded and (not unrecorded.get("run_id") or unrecorded.get("run_id") != cw.get("run_id")
                       or cw.get("done")):
        unrecorded = None
    if cw.get("done") and cw.get("result"):
        r = cw["result"]
        watch = {"reverted": sorted(set(r.get("hot") or []) | set(r.get("restart") or [])),
                 "problems": list(r.get("problems") or []), "run_id": cw.get("run_id", ""),
                 "outcome": r.get("outcome", ""), "reason": r.get("reason", ""),
                 "kept_confirmed": list(r.get("kept_confirmed") or [])}
    watch_worth = bool(watch and (watch["reverted"] or watch["problems"]
                                  or watch["outcome"] in ("inconclusive", "healthy")))
    if not paths and not leftover and not watch_worth and not unrecorded:
        return {}
    return {"paths": paths, "leftover_watch": leftover, "watch": watch if watch_worth else None,
            "watch_unrecorded": unrecorded}


def discard_watch_restores(o, *, apply: bool, out: Callable[[str], None] = print,
                           list_entries: bool = True, dry_run_note: bool = True) -> list[str]:
    """`apply-pending --discard-watch-restores`: drop the `source=watch` restore entries. Returns their paths.

    A dry run (`apply=False`) lists them and changes nothing. Never automatic: the entries are the only record
    of a revert the watch could not finish, and on a host that ran the 2.1.0 watch they are most likely false
    positives (the probe could not run), which is for the operator to confirm.

    `list_entries=False` skips the listing and the "none pending" line (the caller printed them already);
    `dry_run_note=False` skips the "dry run:" line (the caller is about to apply, so it would be untrue)."""
    entries = [e for e in (o.host_restart_pending or []) if e.get("source") == "watch"]
    names = sorted({str(e.get("path", "")) for e in entries})
    if not entries:
        if list_entries:
            out("no `source=watch` restore entries are pending")
        if apply and _close_legacy_watch_result(o):
            out("marked the 2.1.0 watch result inconclusive (discarded by the operator)")
        return []
    for e in (sorted(entries, key=lambda e: str(e.get("path", ""))) if list_entries else []):
        tag = ("queued by a corroborated 2.1.1 failure" if e.get("corroborated")
               else "likely false positive from the 2.1.0 watch")
        out(f"  {e.get('path')}: {tag}")
    if not apply:
        if dry_run_note:
            out(f"dry run: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} would be discarded; "
                "nothing was changed")
        return names
    o.host_restart_pending = [e for e in o.host_restart_pending if e.get("source") != "watch"]
    out(f"discarded {len(entries)} `source=watch` restore entr{'y' if len(entries) == 1 else 'ies'}")
    if _close_legacy_watch_result(o):
        out("marked the 2.1.0 watch result inconclusive (discarded by the operator)")
    return names


def legacy_watch_result_open(o) -> bool:
    """True when a done 2.1.0 watch result still carries `problems` and no `outcome`."""
    cw = getattr(o, "config_watch", None)
    r = cw.get("result") if isinstance(cw, dict) else None
    return bool(isinstance(cw, dict) and cw.get("done") and isinstance(r, dict) and r.get("problems")
                and not r.get("outcome"))


def _close_legacy_watch_result(o) -> bool:
    """A 2.1.0 watch result carries `problems` and no `outcome`. Once the operator discards the restore
    entries it queued, nothing is left to retry, so verify and status must not keep calling it an ERROR.
    The result becomes `inconclusive` (the old problems are kept as `discarded_problems`). Never touches
    the config. True when a result was rewritten."""
    if not legacy_watch_result_open(o):
        return False
    r = o.config_watch["result"]
    r["discarded_problems"] = list(r["problems"])
    r["problems"] = []
    r["outcome"] = "inconclusive"
    r["reason"] = "discarded by the operator"
    return True


def recorded_pending() -> dict:
    """`pending_summary` of setup-state.yaml; {} when it is missing or broken (a status never fails on it)."""
    try:
        from .setup import state as setup_state
        return pending_summary(setup_state.load().openclaw)
    except Exception:  # noqa: BLE001
        return {}


PENDING_COMMAND = "ai-resources openclaw apply-pending"


def read_last_restart(state_path: Path | None = None) -> dict:
    """What the last graceful restart left in the state file and its report ({} when there was none)."""
    at = state_get("last_restart", state_path)
    if not at.isdigit():
        return {}
    out: dict = {"at": int(at), "reason": state_get("last_restart_reason", state_path),
                 "result": state_get("last_restart_result", state_path),
                 "report": state_get("last_report", state_path)}
    counts: dict[str, str] = {}
    try:
        for line in Path(out["report"]).read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k in ("marked_interrupted", "aborted_runs", "recovery_started", "tombstoned",
                     "memory_before", "memory_after"):
                counts[k] = v
    except OSError:
        pass
    out["counts"] = counts
    return out


def restart_summary(knobs: dict[str, str], *, runner: Runner, health: "tuple[int, str]", oc: str,
                    state_path: Path | None = None, now: float | None = None) -> dict:
    """The resource-safety facts `status` shows: mode, window, restarts today, last restart, and the
    classification right now (one sample, no confirmation, health reused). Never raises."""
    now = time.time() if now is None else now
    try:
        verdict = openclaw_pressure.evaluate(
            runner=runner, openclaw_bin=oc, confirm_seconds=0, health=health, env=systemd_env(),
            pressure_pct=knob_number(knobs, "OPENCLAW_RESTART_PRESSURE_PCT"),
            hard_pct=knob_number(knobs, "OPENCLAW_RESTART_HARD_PCT"),
            frozen_min_s=knob_number(knobs, "OPENCLAW_FROZEN_MIN_S"))
        current = {"classification": verdict["classification"],
                   "memory_pct": (verdict["evidence"].get("memory_pct") or [None])[-1],
                   "reason": verdict["evidence"].get("reason", "")}
    except Exception as e:  # noqa: BLE001  (status must keep working whatever the probe does)
        current = {"classification": "unavailable", "memory_pct": None, "reason": type(e).__name__}
    tz = knobs["OPENCLAW_RESTART_TZ"]
    return {"mode": knobs["OPENCLAW_GRACEFUL_RESTART"], "window": knobs["OPENCLAW_RESTART_WINDOW"], "tz": tz,
            "hard_pct": knobs["OPENCLAW_RESTART_HARD_PCT"], "today": restarts_today(now, tz, state_path),
            "cap": int(knob_number(knobs, "OPENCLAW_RESTART_DAILY_CAP")), "current": current,
            "last": read_last_restart(state_path)}

_DURATION_UNITS = (("d", 86400), ("h", 3600), ("min", 60), ("ms", 0.001), ("us", 0.000001), ("s", 1))


def parse_systemd_duration(text: str) -> float | None:
    """"4d 21h 7min 25.721953s" -> seconds; None for anything that is not a systemd time span."""
    total, seen = 0.0, False
    for tok in text.split():
        for unit, mult in _DURATION_UNITS:
            if tok.endswith(unit) and re.fullmatch(r"\d+(\.\d+)?", tok[: -len(unit)] or "x"):
                total += float(tok[: -len(unit)]) * mult
                seen = True
                break
        else:
            return None
    return total if seen else None


def next_elapse_display(realtime: str, monotonic: str, *, uptime_s: float | None, now: float) -> str:
    """The "next" column of a timer. A monotonic-only timer (OnBootSec/OnUnitActiveSec) has an empty
    NextElapseUSecRealtime; its NextElapseUSecMonotonic is a span since boot, so it is converted
    through the uptime (E6). "-" when neither says anything."""
    if realtime.strip():
        return realtime.strip()
    span = parse_systemd_duration(monotonic.strip()) if monotonic.strip() else None
    if span is None or uptime_s is None:
        return "-"
    when = now + (span - uptime_s)
    return time.strftime("%a %Y-%m-%d %H:%M:%S UTC", time.gmtime(when))


def read_uptime(path: str = "/proc/uptime") -> float | None:
    try:
        return float(Path(path).read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def pending_lines(pending: dict) -> list[str]:
    out: list[str] = []
    if pending.get("paths"):
        out.append("restart-required config pending: " + ", ".join(pending["paths"])
                   + f"; run `{PENDING_COMMAND}` when idle")
    if pending.get("leftover_watch"):
        out.append("restore entries queued by the 2.1.0 post-setup watch, likely false positives (it could not run "
                   "its probes): " + ", ".join(pending["leftover_watch"])
                   + f"; inspect with `{PENDING_COMMAND} --discard-watch-restores` (a dry run, nothing changes)")
    u = pending.get("watch_unrecorded")
    if u:
        out.append("a post-setup watch finished and could not record its result: " + (u.get("outcome") or "unknown")
                   + (f" ({u['reason']})" if u.get("reason") else "")
                   + "; check `ai-resources openclaw status` (this note is dropped when a later `ai-resources "
                   "setup` starts its own post-setup watch)")
    w = pending.get("watch")
    if w and w.get("reverted"):
        out.append("the post-setup watch reverted: " + ", ".join(w["reverted"]))
    if w and w.get("problems"):
        out.append("the post-setup watch could not finish its revert: " + "; ".join(w["problems"]))
    if w and w.get("outcome") == "healthy":
        out.append("post-setup watch finished: healthy")
    if w and w.get("outcome") == "inconclusive":
        out.append(f"post-setup watch inconclusive: {w.get('reason') or 'unknown'}; "
                   "nothing was reverted or queued")
    return out


OFFBOX_WARN_AGE_H = 30.0     # the newest off-box daily is older than this: WARN (Q5)
OFFBOX_ERROR_AGE_H = 54.0    # ... and older than this: ERROR


def offbox_uploader_state(home: Path, hostenv: dict, now: float) -> dict:
    """What the uploader's last run left in `~/.openclaw/state/offbox-last.json` (never runs gcloud)."""

    def hours(key: str, default: float) -> float:
        try:
            return float(hostenv.get(key, "") or default)
        except ValueError:
            return default

    path = Path(hostenv.get("OPENCLAW_OFFBOX_STATE") or home / ".openclaw" / "state" / "offbox-last.json")
    out: dict = {"path": str(path), "warn_h": hours("OPENCLAW_OFFBOX_WARN_AGE_H", OFFBOX_WARN_AGE_H),
                 "error_h": hours("OPENCLAW_OFFBOX_ERROR_AGE_H", OFFBOX_ERROR_AGE_H), "reported": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    epoch = data.get("newest_offbox_epoch")
    age = (now - epoch) / 3600 if isinstance(epoch, (int, float)) and not isinstance(epoch, bool) else None
    out.update(reported=True, outcome=str(data.get("outcome", "")), identity=str(data.get("identity", "")),
               reason=str(data.get("reason", "")), at=str(data.get("at", "")),
               newest=str(data.get("newest_offbox", "")), age_hours=age)
    return out


def offbox_uploader_finding(u: dict) -> tuple[str, str]:
    """(level, message) for an `offbox_uploader_state`: ok | warn | error."""
    if not u.get("reported"):
        return "warn", f"the off-box uploader has never reported (no readable state file at {u.get('path')})"
    who = f"uploader last ran as {u.get('identity') or 'an unknown identity'} at {u.get('at') or '?'}"
    age = u.get("age_hours")
    if u.get("outcome") != "ok":
        if age is None:
            return "error", f"the off-box upload fails and no off-box copy is known: {u.get('reason')} ({who})"
        level = "error" if age >= u["error_h"] else "warn"
        return level, (f"the last off-box upload failed: {u.get('reason')}; the newest off-box daily is "
                       f"{age:.0f}h old ({who})")
    if age is None:
        return "warn", f"the off-box uploader reported ok but no newest off-box daily is recorded ({who})"
    if age >= u["error_h"]:
        return "error", f"the newest off-box daily backup is {age:.0f}h old ({who})"
    if age >= u["warn_h"]:
        return "warn", f"the newest off-box daily backup is {age:.0f}h old ({who})"
    return "ok", f"the newest off-box daily backup is {age:.0f}h old ({who})"


def _no_cli(argv, **_kw):
    return 127, ""


def openclaw_rules_report() -> dict:
    """The installed OpenClaw and where the reload rules come from. Two file reads: no CLI, no gateway.

    The version is information only; nothing here compares it with a kit constant.
    """
    rs = openclaw_reload_rules.current_ruleset(_no_cli)
    return {"version": rs.version, "rules_source": rs.source, "rules_count": rs.installed_count,
            "reason": rs.reason, "stricter": list(rs.stricter)}


def collect_status(runner: Runner = default_runner, *, home: Path | None = None,
                   host_env_path: Path | None = None, config_path: Path | None = None,
                   now: float | None = None, run_doctor: bool = True, probe_offbox: bool = True,
                   selection=None) -> dict:
    """Everything `status` prints, as data. Read-only: it never repairs, and sets the two
    environment variables `systemctl --user` needs from a bare shell, which is the manual step
    everyone forgets."""
    home = home or Path.home()
    env = systemd_env()
    hostenv = read_host_env(host_env_path)
    report: dict = {}

    def sc(*args):
        return runner(["systemctl", "--user", *args], env=env)

    rc, active = sc("is-active", GATEWAY_UNIT)
    rc2, enabled = sc("is-enabled", GATEWAY_UNIT)
    report["unit"] = {"active": active.strip() if rc == 0 else (active.strip() or "unknown"),
                      "enabled": enabled.strip() if rc2 == 0 else (enabled.strip() or "unknown")}
    user = env.get("USER") or os.environ.get("USER", "")
    _rc, linger = runner(["loginctl", "show-user", user, "-p", "Linger", "--value"], env=env)
    report["boot"] = {"linger": linger.strip() or "unknown"}
    report["openclaw"] = openclaw_rules_report()

    cfg_file = config_path or Path(os.environ.get("OPENCLAW_CONFIG_PATH") or home / ".openclaw" / "openclaw.json")
    try:
        cfg = json.loads(Path(cfg_file).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        cfg = {}
    port = str(((cfg.get("gateway") or {}).get("port")) or 18789)
    _rc, ss = runner(["ss", "-H", "-ltn"], env=env)
    listeners = [ln.split()[3] for ln in ss.splitlines() if len(ln.split()) >= 4 and ln.split()[3].endswith(f":{port}")]
    report["listeners"] = {"port": port, "addresses": listeners,
                           "wildcard": any(a.startswith(("0.0.0.0", "*", "[::]")) for a in listeners)}

    oc = resolve_openclaw_bin()   # one resolution for health and doctor: a login PATH may lack brew
    rc, _health = runner([oc, "health"], env=env)
    report["health"] = {"ok": rc == 0}
    report["health"]["restart"] = restart_summary(restart_knobs(env={}, host_env=hostenv), runner=runner,
                                                  health=(rc, _health), oc=oc, now=now)

    timers = []
    uptime = read_uptime(os.environ.get("OPENCLAW_UPTIME_FILE") or hostenv.get("OPENCLAW_UPTIME_FILE") or "/proc/uptime")
    for name in TIMER_NAMES:
        _rc, nxt = sc("show", name, "-p", "NextElapseUSecRealtime", "--value")
        mono = ""
        if not nxt.strip():
            _rc, mono = sc("show", name, "-p", "NextElapseUSecMonotonic", "--value")
        rc_e, en = sc("is-enabled", name)
        timers.append({"name": name, "enabled": rc_e == 0 and en.strip() == "enabled",
                       "next": next_elapse_display(nxt, mono, uptime_s=uptime, now=time.time() if now is None else now)})
    report["timers"] = timers

    base = Path(hostenv.get("OPENCLAW_BACKUP_DIR", "/srv/openclaw-backups"))
    tiers = backup_tiers(base, now)
    report["backups"] = tiers

    remote_cmd = hostenv.get("OPENCLAW_OFFBOX_LIST_CMD", "")
    offbox: dict = {"configured": bool(remote_cmd)}
    if remote_cmd and probe_offbox and tiers["daily"].get("present"):
        rc, listing = runner(shlex.split(remote_cmd), env=env)
        offbox.update(ok=rc == 0, present=tiers["daily"]["name"] in listing if rc == 0 else False,
                      name=tiers["daily"]["name"])
    if hostenv.get("OPENCLAW_OFFBOX_BUCKET"):
        offbox["uploader"] = offbox_uploader_state(home, hostenv, time.time() if now is None else now)
    report["off-box"] = offbox

    default_model = model_primary((cfg.get("agents") or {}).get("defaults")) or "?"
    eff = model_pins.effective()
    slots = model_pins.effective_slots(selection) if selection is not None else None
    report["models"] = [{"agent": aid, "model": model_primary(e) or f"{default_model} (default)",
                         "shorthand": isinstance((e or {}).get("model"), str),
                         "drift": model_pins.ref_drift(model_primary(e) or "", eff, slots)}
                        for aid, e in ((cfg.get("agents") or {}).get("entries") or {}).items()]
    # The defaults and the heartbeat are references too; a row appears only when one lags the pins.
    defaults = (cfg.get("agents") or {}).get("defaults") or {}
    for label, owner in (("(defaults)", defaults), ("(defaults heartbeat)", defaults.get("heartbeat"))):
        ref = model_primary(owner if isinstance(owner, dict) else {})
        if ref and model_pins.ref_drift(ref, eff, slots):
            report["models"].append({"agent": label, "model": ref, "shorthand": False,
                                     "drift": model_pins.ref_drift(ref, eff, slots)})

    report["identity"] = agent_identities(cfg)
    report["stability"] = stability_summary(home / ".openclaw" / "logs" / "stability", now)
    if not run_doctor:
        report["doctor"] = {"ran": False, "status": "skipped"}
        return report
    rc, doctor = runner([oc, "doctor", "--non-interactive"], env=env, timeout=DOCTOR_TIMEOUT)
    noise, signal = filter_doctor_warnings(doctor) if rc == 0 or doctor else ([], [])
    status = "ok" if rc == 0 else "missing" if rc == 127 else "timeout" if rc == 124 else "failed"
    report["doctor"] = {"ran": rc == 0, "status": status, "rc": rc, "bin": oc,
                        "noise": len(noise), "signal": signal}
    return report


JOURNAL_FINDING_MAX_HOSTS = 5
_CLOSE_FAILED_SUFFIXES = ("restart_close_failed", "stop_close_failed")


def journal_findings(runner: Runner, stability: dict | None) -> list[tuple[str, str, str]]:
    """(level, message, remedy) findings read from the last 24 h of the gateway journal. Read-only and
    bounded; silent (an empty list) when the journal cannot be read. Used by `status` and `verify`."""
    signals = openclaw_pressure.read_journal_signals(runner)
    if not signals.get("available"):
        return []
    out: list[tuple[str, str, str]] = []
    hosts = signals["tls_hosts"]
    if hosts:
        shown = hosts[:JOURNAL_FINDING_MAX_HOSTS]
        more = f" (+{len(hosts) - len(shown)} more)" if len(hosts) > len(shown) else ""
        out.append(("warn",
                    f"the secret egress proxy refused {len(hosts)} host(s) in the last "
                    f"{openclaw_pressure.JOURNAL_SIGNAL_HOURS} h with `TLS certificate unavailable`: "
                    f"{', '.join(openclaw_pressure.printable(h) for h in shown)}{more}",
                    "any process that writes to the unit's journal can cause this line, and it is not the traffic "
                    "allowlist: add a host to secrets.egressProxy.bypassHosts only if you recognise it and "
                    "understand that it then skips the secret egress proxy (a restart-required key: apply it in "
                    "a window, see pitfall T44 and the runbook)"))
    stab = stability or {}
    reason = openclaw_pressure.printable(stab.get("reason") or "")
    age = stab.get("age_hours")
    recent = isinstance(age, (int, float)) and age <= openclaw_pressure.JOURNAL_SIGNAL_HOURS
    newest_failed = bool(stab.get("present") and recent and reason.endswith(_CLOSE_FAILED_SUFFIXES))
    # the newest bundle is not always the failed one: any close-failed bundle journalled in the last 24 h counts
    n_failed = signals.get("close_failed", 0) + (0 if signals.get("close_failed") else int(newest_failed))
    if n_failed and signals["completions"] > 0:
        when = f"a `{reason}` stability bundle from {age:.0f} h ago" if newest_failed \
            else f"{n_failed} close-failed stability bundle(s) in the last {openclaw_pressure.JOURNAL_SIGNAL_HOURS} h"
        out.append(("warn",
                    f"{when}, and "
                    f"{signals['completions']} subagent completion line(s) deferred or lost in the journal",
                    "a restart or stop with work in flight loses subagent results: see T39 and T41; restart "
                    "with `ai-resources openclaw graceful-restart` and re-run what was lost"))
    return out


def _render_stability(s: dict) -> str:
    if not s.get("present"):
        return "stability   no bundles"
    if s.get("error"):
        return f"stability   newest bundle {s['name']} could not be read ({s['error']})"
    import datetime as dt
    when = dt.datetime.fromtimestamp(s["generated_at"], dt.timezone.utc).strftime("%Y-%m-%d %H:%M") \
        if s.get("generated_at") is not None else "?"
    ago = f" ({_humanize(s['age_hours'] * 3600)} ago)" if s.get("age_hours") is not None else ""
    head = f"stability   newest {s['reason']} {when}{ago}: "
    if not s["stalled"]:
        return head + "no stalled sessions"
    parts = []
    for reason, n in sorted(s["stalled"].items(), key=lambda kv: -kv[1]):
        tools = f": {', '.join(sorted(s['tools']))}" if reason == "blocked_tool_call" and s["tools"] else ""
        parts.append(f"{reason} {n}{tools}")
    dropped = f", {s['dropped']} events dropped" if s["dropped"] else ""
    return (head + f">={sum(s['stalled'].values())} stalled ({'; '.join(parts)}), "
            f"oldest {round(s['max_stalled_age_s'] / 60)} min{dropped} (counts are lower bounds); see T39")


def _render_restart(r: dict | None) -> list[str]:
    if not r:
        return []
    cur = r["current"]
    now = cur["classification"] + (f" ({cur['memory_pct']}% of MemoryHigh)" if cur.get("memory_pct") is not None else "")
    if cur["classification"] in ("refused-probe", "unavailable") and cur.get("reason"):
        now += f" ({cur['reason']}; nothing acts on an unreadable signal)"
    out = [f"  resource  mode {r['mode']}; window {r['window'] or 'none'} {r['tz']}, ceiling {r['hard_pct']}% of "
           f"MemoryHigh; {r['today']}/{r['cap'] or 'no cap'} restarts today; now: {now}"]
    last = r.get("last") or {}
    if last:
        when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(last["at"]))
        c = last.get("counts") or {}
        freed = ""
        if c.get("memory_before", "").isdigit() and c.get("memory_after", "").isdigit():
            freed = f", memory {int(c['memory_before']) / 1024 ** 3:.1f} -> {int(c['memory_after']) / 1024 ** 3:.1f} GiB"
        rec = (f", recovery: {c.get('marked_interrupted', '?')} marked, {c.get('aborted_runs', '?')} aborted, "
               f"{c.get('recovery_started', '?')} resumed, {c.get('tombstoned', '?')} tombstoned") if c else ""
        out.append(f"  last      {when} reason {last['reason'] or '?'}: {last['result'] or '?'}{freed}{rec}"
                   + (f"; report {last['report']}" if last.get("report") else ""))
    return out


def _render_openclaw(o: dict | None) -> list[str]:
    if not o:
        return []
    version = o.get("version") or "version unknown"
    if o.get("rules_source") != "installed":
        return [f"openclaw    {version}; reload rules NOT readable ({o.get('reason') or 'unknown reason'}): "
                "every config key is treated as restart-required"]
    out = [f"openclaw    {version}; reload rules read from the installed package ({o.get('rules_count', 0)} rules)"]
    if o.get("stricter"):
        out.append("            kit keeps restart-required: "
                   + ", ".join(f"{p} (installed OpenClaw says hot)" for p in o["stricter"]))
    return out


def render_status(report: dict) -> str:
    lines = ["OpenClaw host status", ""]
    u, b = report["unit"], report["boot"]
    lines.append(f"unit        {GATEWAY_UNIT}: {u['active']}, {u['enabled']}")
    lines.append(f"boot        linger: {b['linger']}")
    lines += _render_openclaw(report.get("openclaw"))
    ls = report["listeners"]
    addr = ", ".join(ls["addresses"]) or "nothing listening"
    lines.append(f"listeners   port {ls['port']}: {addr}" + ("   ATTENTION: bound to all interfaces (T04)" if ls["wildcard"] else ""))
    lines.append(f"health      {'answers' if report['health']['ok'] else 'DOES NOT ANSWER'}")
    lines += _render_restart(report["health"].get("restart"))
    lines.append("timers")
    for t in report["timers"]:
        lines.append(f"  {t['name']:<34} {'enabled' if t['enabled'] else 'OFF':<8} next {t['next']}")
    lines.append("backups")
    for tier, info in report["backups"].items():
        if not info["present"]:
            lines.append(f"  {tier:<8} none")
            continue
        flag = "   STALE" if info["stale"] else ""
        chk = "" if info["checksum"] else "   NO .sha256"
        lines.append(f"  {tier:<8} {_humanize(info['age_hours'] * 3600)} old, {_size(info['size'])}, "
                     f"{info['count']} on disk{flag}{chk}")
    ob = report["off-box"]
    if not ob["configured"] and ob.get("uploader") is not None:
        lines.append("off-box     no listing command (set OPENCLAW_OFFBOX_LIST_CMD to also check presence)")
    elif not ob["configured"]:
        lines.append("off-box     not configured (set OPENCLAW_OFFBOX_LIST_CMD in kit-host.env)")
    elif "ok" not in ob:
        lines.append("off-box     no local daily to look for")
    else:
        lines.append("off-box     " + (f"newest daily {ob['name']} is present" if ob["present"]
                                       else f"newest daily {ob['name']} is MISSING off-box"
                                       if ob["ok"] else "the listing command failed"))
    if ob.get("uploader") is not None:
        level, msg = offbox_uploader_finding(ob["uploader"])
        lines.append(f"off-box up  {'' if level == 'ok' else level.upper() + ': '}{msg}")
    lines.append("models")
    for m in report["models"]:
        lines.append(f"  {m['agent']:<16} {m['model']}"
                     + (f"   DRIFT: the pins say {m['drift']} (`ai-resources models status`)" if m.get("drift") else ""))
    short = [m for m in report["models"] if m.get("shorthand")]
    if short:
        lines.append("  the short form of `model` is legal and the kit reads both; to spell it as an object:")
        lines += [f"    openclaw config set agents.entries.{m['agent']}.model.primary {m['model']}" for m in short]
    lines.append("identity")
    ident = report.get("identity", [])
    for i in ident:
        lines.append(f"  {i['agent']:<16} {i['name'] or '-':<12} {i['workspace']}   IDENTITY.md: {i['file_name'] or '-'}")
    by_ws: dict[str, list[dict]] = {}
    for i in ident:
        by_ws.setdefault(i["workspace"], []).append(i)
    for ws, group in by_ws.items():
        unnamed = [i["agent"] for i in group if not i["name"]]
        if len(group) > 1 and unnamed:
            lines.append(f"  ATTENTION: {', '.join(i['agent'] for i in group)} share {ws}; "
                         f"{', '.join(unnamed)} ha{'s' if len(unnamed) == 1 else 've'} no identity.name, so OpenClaw "
                         f"narrates it with IDENTITY.md's name ({group[0]['file_name'] or 'none'}). The kit never writes that file; "
                         + "; ".join(f"openclaw config set agents.entries.{a}.identity.name {a} --dry-run" for a in unnamed))
    lines.append(_render_stability(report.get("stability") or {"present": False}))
    d = report["doctor"]
    if d.get("status") == "skipped":
        lines.append("doctor      not run (--no-doctor)")
    elif d.get("status") == "missing":
        lines.append(f"doctor      not run: {d.get('bin', 'openclaw')} was not found "
                     "(put the openclaw binary on PATH, e.g. /home/linuxbrew/.linuxbrew/bin)")
    elif d.get("status") == "timeout":
        lines.append(f"doctor      timed out after {DOCTOR_TIMEOUT}s (re-run `ai-resources openclaw doctor`)")
    elif d.get("status") == "failed":
        lines.append(f"doctor      failed (rc={d.get('rc')})")
    elif not d["signal"]:
        lines.append(f"doctor      clean ({d['noise']} known-noise warning(s) filtered, T30)")
    else:
        lines.append(f"doctor      {len(d['signal'])} warning(s) not in the known-noise catalogue:")
        lines += [f"  {w}" for w in d["signal"]]
    return "\n".join(lines)


# --- CLI ------------------------------------------------------------------------------------------

def cmd_install_units(args: argparse.Namespace) -> int:
    if args.render_only:
        for name, text in render_units().items():
            print(f"=== {name}\n{text}", end="" if text.endswith("\n") else "\n")
        return 0
    result = install_units(Path(args.dest) if args.dest else None, dry_run=args.dry_run,
                           enable=args.enable)
    for name in result["changed"]:
        print(("would write " if args.dry_run else "wrote ") + name)
    print(f"{len(result['unchanged'])} unit(s) already up to date")
    if result["reloaded"]:
        print("systemd reloaded")
    for timer in result["enabled"]:
        print(f"enabled {timer}")
    return 0


def cmd_render_gitops_backups(args: argparse.Namespace) -> int:
    if args.list_markers:
        print("\n".join(gitops_marker_names()))
        return 0
    markers: dict[str, str] = {}
    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep or not key:
            print(f"error: --set wants KEY=VALUE, got {item!r}")
            return 2
        markers[key] = value
    try:
        rendered = render_gitops_backups(markers)
    except (KeyError, ValueError) as e:
        print(f"error: {e.args[0] if e.args else e}")
        return 2
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for name, text in rendered.items():
            (out / name).write_text(text, encoding="utf-8")
            print(f"wrote {out / name}")
    else:
        for name, text in rendered.items():
            print(f"# === {name}\n{text}", end="" if text.endswith("\n") else "\n")
    return 0


# --- the graceful restart (ADR-0004) -------------------------------------------------------------------------
#
# The ONE self-initiated restart of the gateway. Gates, action, snapshots and report live together here so the
# operator's CLI and the health timer cannot disagree about when it is allowed. Signal reading is
# `openclaw_pressure`; scheduling, the mode and the notice are the health-restart script. It never passes
# `--force`, never kills anything and never edits openclaw.json.

EXIT_REFUSED_SELF = 8        # the caller is a child of the gateway cgroup: its own restart would kill it (T29)
EXIT_LOCKED = 9              # another graceful restart (timer or manual) holds the lock
EXIT_REFUSED_GATE = 10       # a gate said no; the gate's name is in the JSON
EXIT_RESTART_FAILED = 11     # `openclaw gateway restart` returned non-zero
EXIT_NO_NEW_PID = 12         # health answered but the MainPID did not change: nothing was restarted
EXIT_HEALTH_TIMEOUT = 13     # the command returned 0 but health did not answer in time
EXIT_RESTART_REFUSED = 14    # the gateway refused to prepare the restart on every attempt

RESTART_COMMAND_TIMEOUT_S = 600
RESTART_COMMAND_GRACE_S = 30       # the runner's own timeout is `timeout 600` plus this, as a backstop
RESTART_HEALTH_TIMEOUT_S = 180
RESTART_RETRY_BACKOFF_S = (20, 40, 80)
# Time budget (ADR-0004, "Time budget"). Every phase has a monotonic bound, so the worst case is a sum and
# not a hope; the timer unit's TimeoutStartSec (templates/systemd/openclaw-health-restart.service.template)
# must stay above it:
#   script, before the CLI    is-active 30 + journal 30 + pressure sample (`timeout`) 300  =  360 s
#   restart attempts+backoffs RESTART_PHASE_BUDGET_S (an attempt starts only if it can end inside it) = 900 s
#   health poll               RESTART_HEALTH_TIMEOUT_S + HEALTH_FINAL_PROBE_MIN_S             =  185 s
#   settle                    OPENCLAW_RESTART_SETTLE_S, clipped to RESTART_SETTLE_MAX_S      <= 180 s
#   bookkeeping probes        at most 18 (each capped at PROBE_CAP_S) + the confirm sample
#                             that a run without a classification takes (60 + 6 probes)       =  780 s
#   notices (send_result)     3 x (30 + 20) + one notify_once 30                              =  180 s
#   total                                                                                     = 2585 s
# TimeoutStartSec=50min is 3000 s: a margin of about 7 minutes.
RESTART_PHASE_BUDGET_S = 900
RESTART_SETTLE_MAX_S = 180
PROBE_CAP_S = 30
REFUSAL_CODE = "GATEWAY_RESTART_PREPARATION_REFUSED"
SNAPSHOT_FILES = ("memory.current", "memory.high", "memory.max", "memory.events", "memory.stat",
                  "memory.pressure", "pids.current")
SESSION_SAFE_KEYS = ("key", "agentId", "kind", "status", "model", "channel", "updatedAt", "ageMs")

STATE_PATH = Path.home() / ".openclaw" / "health-restart.state"
LOCK_PATH = Path.home() / ".openclaw" / "health-restart.lock"
SNAPSHOT_ROOT = Path.home() / ".openclaw" / "logs" / "restart-snapshots"


class MarkerGuard:
    """`watchdog.off` held for exactly the life of a `with` block, on EVERY exit path.

    Normal exit and an exception are the `finally`. SIGTERM (what systemd sends at TimeoutStopSec or a
    `systemctl stop` of the timer's service) and SIGINT are turned into an exception by a handler, so the
    same `finally` removes the file; the previous handlers are restored afterwards. Handlers are only
    installed from the main thread. SIGKILL cannot be caught: `verify` flags a stale marker (ADR-0004).
    """

    def __init__(self, marker: Marker | None = None, *, signals: tuple[int, ...] | None = None,
                 exclusive: bool = False):
        import signal
        self.exclusive = exclusive
        self.marker = marker or Marker()
        self._signals = signals if signals is not None else (signal.SIGTERM, signal.SIGINT)
        self._previous: dict[int, object] = {}
        self._owned = False

    def __enter__(self) -> "MarkerGuard":
        import signal
        import threading

        def _raise(signum, _frame):
            raise SystemExit(128 + signum)

        if threading.current_thread() is threading.main_thread():
            for sig in self._signals:
                self._previous[sig] = signal.signal(sig, _raise)
        # The marker is created with O_EXCL: a window the operator (or another path) already opened is not ours
        # to close, and `exclusive=True` refuses to run inside it at all.
        try:
            self._owned = self.marker.acquire()
        except BaseException:
            # a signal delivered as the mask was lifted: the file is ours and its identity is recorded
            try:
                if self.marker.acquired:
                    self.marker.remove()
            finally:
                self._restore()
            raise
        if self.exclusive and not self._owned:
            self._restore()
            raise WindowBusy(f"{self.marker.path} exists: a maintenance window is open")
        return self

    @property
    def owned(self) -> bool:
        """True when this guard created the marker (and so removes it)."""
        return self._owned

    def _restore(self) -> None:
        import signal
        for sig, handler in self._previous.items():
            signal.signal(sig, handler)  # type: ignore[arg-type]
        self._previous.clear()

    def __exit__(self, *_exc) -> None:
        try:
            if self._owned:
                self.marker.remove()
        finally:
            self._restore()


def state_get(key: str, path: Path | None = None) -> str:
    """The last `key=value` line for `key` in the health-restart state file ("" when absent)."""
    try:
        lines = (path or STATE_PATH).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    found = ""
    for line in lines:
        if line.startswith(key + "="):
            found = line.partition("=")[2]
    return found


def state_set(key: str, value: str, path: Path | None = None) -> None:
    """Set `key` keeping every other line. Atomic (tmp + rename), the same shape the script's `setv` writes."""
    path = path or STATE_PATH
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.startswith(key + "=")]
    except OSError:
        lines = []
    lines.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _local_date(now: float, tz: str) -> str:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(now, tz=ZoneInfo(tz)).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return time.strftime("%Y-%m-%d", time.gmtime(now))


def restarts_today(now: float, tz: str, state_path: Path | None = None) -> int:
    day, _, n = state_get("restarts_day", state_path).partition(":")
    return int(n) if day == _local_date(now, tz) and n.isdigit() else 0


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def _safe_sessions(text: str) -> str:
    """A bounded, prompt-free view of `openclaw sessions list --json`: only whitelisted scalar fields."""
    try:
        data = json.loads(text)
    except ValueError:
        return json.dumps({"unparsed_bytes": len(text.encode("utf-8"))})
    items = data if isinstance(data, list) else (data.get("sessions") if isinstance(data, dict) else None)
    if not isinstance(items, list):
        return json.dumps({"unrecognised_shape": type(data).__name__})
    slim = [{k: v for k, v in it.items() if k in SESSION_SAFE_KEYS and isinstance(v, (str, int, float, bool))}
            for it in items[:200] if isinstance(it, dict)]
    return json.dumps({"count": len(items), "sessions": slim}, indent=1)


def take_snapshot(dest: Path, *, runner: Runner, control_group: str, cgroup_root: Path = CGROUP_ROOT,
                  proc_root: Path = PROC_ROOT, openclaw_bin: str = "openclaw", with_sessions: bool = True) -> None:
    """Write the evidence of one moment into `dest` (0700 dir, 0600 files). No process argv, no prompt text."""
    _private_dir(dest)
    env = systemd_env()
    busy = gateway_busy_strict(runner, cgroup_root=cgroup_root, proc_root=proc_root)
    _write_private(dest / "busy.txt", "unknown\n" if busy is None else f"{busy}\n")
    _rc, unit = runner(["systemctl", "--user", "show", GATEWAY_UNIT, "-p", "MainPID", "-p", "MemoryCurrent",
                        "-p", "MemoryHigh", "-p", "ActiveState", "-p", "SubState", "-p", "ActiveEnterTimestamp",
                        "-p", "NRestarts"], env=env)
    _write_private(dest / "unit.txt", unit + "\n")
    _rc, free = runner(["free", "-b"])
    _write_private(dest / "free.txt", free + "\n")
    cg = cgroup_root / control_group.lstrip("/")
    parts = []
    for name in SNAPSHOT_FILES:
        try:
            parts.append(f"== {name}\n{(cg / name).read_text(encoding='utf-8').strip()}\n")
        except OSError:
            parts.append(f"== {name}\n(unreadable)\n")
    _write_private(dest / "cgroup.txt", "".join(parts))
    try:
        pids = [p for p in (cg / "cgroup.procs").read_text(encoding="utf-8").split() if p.isdigit()][:2000]
    except OSError:
        pids = []
    if pids:
        # comm only: `args`/`cmd` can carry prompt text or tokens, so they are never asked for.
        _rc, ps = runner(["ps", "-o", "pid,ppid,stat,etimes,rss,comm", "-p", ",".join(pids)])
        _write_private(dest / "ps.txt", ps + "\n")
    if with_sessions:
        _rc, sessions = runner(["timeout", "30", openclaw_bin, "sessions", "list", "--json"], env=env, timeout=40)
        _write_private(dest / "sessions.json", _safe_sessions(sessions) + "\n")


def prune_snapshots(root: Path, keep: int) -> list[str]:
    """Keep the newest `keep` snapshot directories (names sort by timestamp). Returns what was removed."""
    try:
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
    removed = []
    for old in dirs[: max(0, len(dirs) - max(1, keep))]:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old.name)
    return removed


def _gateway_active_state(runner: Runner) -> str:
    rc, out = runner(["systemctl", "--user", "is-active", GATEWAY_UNIT], env=systemd_env())
    return out.strip() or ("active" if rc == 0 else "unknown")


def _lock_held_by_other(path: Path) -> bool:
    """Read-only check used by --dry-run (it must not create the lock file)."""
    import fcntl
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def graceful_restart(*, reason: str = "manual", dry_run: bool = False, classification: str | None = None,
                     runner: Runner = default_runner, knobs: dict[str, str] | None = None,
                     marker: Marker | None = None, state_path: Path | None = None, lock_path: Path | None = None,
                     snapshot_root: Path | None = None, cgroup_root: Path = CGROUP_ROOT,
                     proc_root: Path = PROC_ROOT, self_cgroup_path: Path | str = "/proc/self/cgroup",
                     now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                     openclaw_bin: str | None = None, out: Callable[[str], None] = print,
                     clock: Callable[[], float] = time.monotonic) -> dict:
    """Restart the gateway gracefully under confirmed memory pressure, or say which gate refused.

    Returns a JSON-able dict with `exit_code`, `result` (ok | failed | refused | refused-gate |
    refused-self | locked | dry-run) and the evidence. The fixed order is:

      1. refuse when this process is inside the gateway cgroup (EXIT_REFUSED_SELF)
      2. take a non-blocking flock (EXIT_LOCKED)
      3. gates: watchdog.off, unit active, mode, pressure, cooldown, daily cap, window or hard ceiling
      4. `dry_run`: report every verdict and stop, with no state, marker, snapshot or restart
      5. state BEFORE acting, pre-snapshot, `timeout 600 openclaw gateway restart` inside MarkerGuard
         (retried on PREPARATION_REFUSED with a 20/40/80 s backoff, all inside RESTART_PHASE_BUDGET_S),
         success = health (a wall-clock budget, probes included) plus a NEW MainPID
      6. settle, post-snapshot, journal report, state, summary
    """
    knobs = dict(knobs) if knobs is not None else restart_knobs()
    marker = marker or Marker()
    state_path = state_path or STATE_PATH
    lock_path = lock_path or LOCK_PATH
    snapshot_root = snapshot_root or SNAPSHOT_ROOT
    oc = openclaw_bin or resolve_openclaw_bin()
    manual = reason == "manual"
    result: dict = {"reason": reason, "dry_run": dry_run, "gates": [], "result": "", "exit_code": EXIT_OK}

    def finish(code: int, outcome: str, **extra) -> dict:
        result.update(exit_code=code, result=outcome, **extra)
        return result

    # 1. never from inside the gateway cgroup
    cg = gateway_cgroup_state(self_cgroup_path)
    if cg == "inside":
        out("refusing: this process runs inside the gateway cgroup; its own restart would kill it (T29)")
        return finish(EXIT_REFUSED_SELF, "refused-self")
    if cg == "unknown":
        out(CGROUP_UNKNOWN_LINE)
        return finish(EXIT_REFUSED_SELF, "refused-self")

    # 2. one restart path at a time: the timer, a manual run, the drained window and setup share the lock
    lock_fd = None
    if dry_run:
        if _lock_held_by_other(lock_path):
            return finish(EXIT_LOCKED, "locked")
    else:
        try:
            lock_fd = acquire_restart_lock(lock_path)
        except RestartLockUnusable as e:
            out(f"refusing: the restart lock file cannot be used ({e}); remove it or fix its permissions")
            return finish(EXIT_LOCKED, "locked")
        if lock_fd is None:
            out("refusing: another restart or maintenance window is in progress (the restart lock is held); "
                "nothing was stopped.")
            return finish(EXIT_LOCKED, "locked")
    try:
        try:
            return _graceful_restart_locked(result, finish, reason=reason, manual=manual, dry_run=dry_run,
                                            classification=classification, runner=runner, knobs=knobs,
                                            marker=marker, state_path=state_path, snapshot_root=snapshot_root,
                                            cgroup_root=cgroup_root, proc_root=proc_root, now=now, sleep=sleep,
                                            oc=oc, out=out, clock=clock)
        except Exception as e:  # noqa: BLE001  (MarkerGuard already removed watchdog.off)
            if not dry_run:
                state_set("last_restart_result", "failed", state_path)
            out(f"graceful restart failed: {type(e).__name__}")
            return finish(EXIT_RESTART_FAILED, "failed", error=type(e).__name__)
    finally:
        release_restart_lock(lock_fd)       # the lock file is never unlinked (see `acquire_restart_lock`)


def _graceful_restart_locked(result: dict, finish, *, reason: str, manual: bool, dry_run: bool,
                             classification: str | None, runner: Runner, knobs: dict[str, str], marker: Marker,
                             state_path: Path, snapshot_root: Path, cgroup_root: Path, proc_root: Path,
                             now: Callable[[], float], sleep: Callable[[float], None], oc: str,
                             out: Callable[[str], None],
                             clock: Callable[[], float] = time.monotonic) -> dict:
    raw_runner = runner

    def runner(argv, **kw):  # noqa: F811  (bookkeeping probes: capped so the time budget above holds)
        t = kw.get("timeout", 120)
        kw["timeout"] = PROBE_CAP_S if t is None else min(t, PROBE_CAP_S)
        return raw_runner(argv, **kw)

    t_now = now()
    tz = knobs["OPENCLAW_RESTART_TZ"]
    gates: list[dict] = result["gates"]

    def gate(name: str, ok: bool, detail: str) -> bool:
        gates.append({"name": name, "ok": ok, "detail": detail})
        return ok

    # --- gates (all evaluated in dry-run; the first failure stops a real run) ---
    gate("watchdog_off", not marker.exists(),
         "a maintenance window is open (watchdog.off)" if marker.exists() else "absent")
    state = _gateway_active_state(runner)
    gate("unit_active", state == "active", f"unit is {state}")
    mode = knobs["OPENCLAW_GRACEFUL_RESTART"]
    gate("mode", manual or mode == "on", f"mode={mode}" + (" (manual run)" if manual else ""))
    if not manual:
        if classification is None:
            knob_confirm = knob_number(knobs, "OPENCLAW_HEALTH_CONFIRM_S")
            verdict = openclaw_pressure.evaluate(
                runner=runner, openclaw_bin=oc, cgroup_root=cgroup_root, proc_root=proc_root,
                confirm_seconds=knob_confirm, sleep=sleep, clock=now, env=systemd_env(),
                pressure_pct=knob_number(knobs, "OPENCLAW_RESTART_PRESSURE_PCT"),
                hard_pct=knob_number(knobs, "OPENCLAW_RESTART_HARD_PCT"),
                frozen_min_s=knob_number(knobs, "OPENCLAW_FROZEN_MIN_S"))
            classification = verdict["classification"]
        gate("pressure", classification in ("pressure", "hard"), f"classification={classification}")
    result["classification"] = classification
    last = state_get("last_restart", state_path)
    cooldown = int(knob_number(knobs, "OPENCLAW_RESTART_COOLDOWN_S"))
    since = t_now - int(last) if last.isdigit() else None
    gate("cooldown", since is None or since >= cooldown,
         "no earlier restart" if since is None else f"last restart {int(since)}s ago, cooldown {cooldown}s")
    cap = int(knob_number(knobs, "OPENCLAW_RESTART_DAILY_CAP"))
    today = restarts_today(t_now, tz, state_path)
    gate("daily_cap", cap <= 0 or today < cap, f"{today} restart(s) today, cap {cap or 'none'}")
    in_window = openclaw_pressure.window_open(t_now, knobs["OPENCLAW_RESTART_WINDOW"], tz)
    gate("window", in_window or classification == "hard",
         ("inside the window" if in_window else
          f"outside {knobs['OPENCLAW_RESTART_WINDOW'] or '(no window)'} {tz}")
         + (", hard ceiling overrides" if (not in_window and classification == "hard") else ""))

    failed = [g for g in gates if not g["ok"]]
    if failed:
        result["gate"] = failed[0]["name"]
        out("refusing: " + "; ".join(f"{g['name']}: {g['detail']}" for g in failed))
        return finish(EXIT_REFUSED_GATE, "refused-gate")
    if dry_run:
        result["planned"] = ["write state", "pre-snapshot", "touch watchdog.off",
                             f"timeout {RESTART_COMMAND_TIMEOUT_S} {oc} gateway restart",
                             f"poll health up to {RESTART_HEALTH_TIMEOUT_S}s and require a new MainPID",
                             "remove watchdog.off", f"settle {knobs['OPENCLAW_RESTART_SETTLE_S']}s",
                             "post-snapshot and recovery report"]
        for step in result["planned"]:
            out("  would: " + step)
        return finish(EXIT_OK, "dry-run")

    # --- act: state BEFORE the restart, so a crash leaves the cooldown in force ---
    t0 = t_now
    state_set("last_restart", str(int(t0)), state_path)
    state_set("restarts_day", f"{_local_date(t0, tz)}:{today + 1}", state_path)
    state_set("last_restart_reason", reason, state_path)
    state_set("last_restart_result", "started", state_path)

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t0))
    snap = snapshot_root / stamp
    group = ""
    rc_g, grp = runner(["systemctl", "--user", "show", GATEWAY_UNIT, "-p", "ControlGroup", "--value"],
                       env=systemd_env())
    if rc_g == 0:
        group = grp.strip()
    before = None
    try:
        before = openclaw_pressure.read_cgroup(group, cgroup_root)["current"] if group else None
    except openclaw_pressure.ProbeError:
        before = None
    result.update(snapshot_dir=str(snap), memory_before=before)
    _private_dir(snapshot_root)
    _private_dir(snap)
    take_snapshot(snap / "before", runner=runner, control_group=group, cgroup_root=cgroup_root,
                  proc_root=proc_root, openclaw_bin=oc)
    prune_snapshots(snapshot_root, int(knob_number(knobs, "OPENCLAW_RESTART_SNAPSHOTS_KEEP")))

    old_pid = gateway_main_pid(runner)
    result["old_pid"] = old_pid
    attempts = 0
    rc = -1
    refused = False
    started = clock()
    phase_end = started + RESTART_PHASE_BUDGET_S
    health_ok = False
    try:
        guard = MarkerGuard(marker, exclusive=True)
        guard.__enter__()
    except WindowBusy:
        # an operator opened a window between the gate and here: the restart does not run inside it
        state_set("last_restart_result", "refused-gate", state_path)
        result["gate"] = "watchdog_off"
        out("refusing: a maintenance window (watchdog.off) was opened while the restart was being prepared")
        return finish(EXIT_REFUSED_GATE, "refused-gate")
    try:
        for backoff in (0, *RESTART_RETRY_BACKOFF_S):
            if backoff:
                # A further attempt starts only if backoff + the longest attempt still end inside the
                # phase budget, so the budget bounds the marker's lifetime (never a half-run restart).
                if phase_end - clock() < backoff + RESTART_COMMAND_TIMEOUT_S + RESTART_COMMAND_GRACE_S:
                    result["budget_exhausted"] = True
                    break
                sleep(backoff)
            attempts += 1
            rc, text = raw_runner(["timeout", str(RESTART_COMMAND_TIMEOUT_S), oc, "gateway", "restart"],
                                  env=systemd_env(), timeout=RESTART_COMMAND_TIMEOUT_S + RESTART_COMMAND_GRACE_S)
            refused = rc != 0 and REFUSAL_CODE in (text or "")
            if not refused:
                break
        result["restart_rc"], result["attempts"] = rc, attempts
        result["restart_seconds"] = int(clock() - started)
        if rc == 0:
            health_ok = poll_health(raw_runner, oc, env=systemd_env(), timeout=RESTART_HEALTH_TIMEOUT_S,
                                    interval=5, sleep=sleep, clock=clock)
        new_pid = gateway_main_pid(runner)
        result["new_pid"] = new_pid
    finally:
        guard.__exit__(None, None, None)
    # watchdog.off is gone from here on, on every path above.

    if refused:
        code, outcome, label = EXIT_RESTART_REFUSED, "refused", "refused"
    elif rc != 0:
        code, outcome, label = EXIT_RESTART_FAILED, "failed", "failed"
    elif not health_ok:
        code, outcome, label = EXIT_HEALTH_TIMEOUT, "failed", "failed"
    elif not new_pid or new_pid == old_pid:
        code, outcome, label = EXIT_NO_NEW_PID, "failed", "failed"
    else:
        code, outcome, label = EXIT_OK, "ok", "ok"

    settle = (min(int(knob_number(knobs, "OPENCLAW_RESTART_SETTLE_S")), RESTART_SETTLE_MAX_S)
              if code == EXIT_OK else 0)
    if settle:
        sleep(settle)
    after = None
    try:
        after = openclaw_pressure.read_cgroup(group, cgroup_root)["current"] if group else None
    except openclaw_pressure.ProbeError:
        after = None
    take_snapshot(snap / "after", runner=runner, control_group=group, cgroup_root=cgroup_root,
                  proc_root=proc_root, openclaw_bin=oc, with_sessions=False)
    journal = openclaw_pressure.read_report(str(int(t0)), runner)
    result.update(memory_after=after, recovery=journal)
    report = snap / "report.txt"
    lines = [f"restart {stamp} reason={reason} result={label} exit={code}",
             f"command_rc={rc} attempts={attempts} old_pid={old_pid} new_pid={result.get('new_pid', '')}",
             f"restart_command_seconds={result.get('restart_seconds')}",
             f"memory_before={before} memory_after={after}"]
    lines += [f"{k}={v}" for k, v in journal.items()]
    _write_private(report, "\n".join(lines) + "\n")
    state_set("last_restart_result", label, state_path)
    state_set("last_report", str(report), state_path)
    result["report"] = str(report)
    out(f"graceful restart {label}: attempts={attempts} old_pid={old_pid} new_pid={result.get('new_pid', '')}")
    return finish(code, outcome)


def cmd_graceful_restart(args: argparse.Namespace, **kw) -> int:
    res = graceful_restart(reason=args.reason, dry_run=args.dry_run, classification=args.classification,
                           out=(lambda _m: None) if args.json else print, **kw)
    if args.json:
        print(json.dumps(res, sort_keys=True))
    else:
        print(f"{res['result']} (exit {res['exit_code']})" + (f" gate={res['gate']}" if res.get("gate") else ""))
    return int(res["exit_code"])



def cmd_doctor(args: argparse.Namespace) -> int:
    """`doctor`: stops the gateway, so it needs a terminal or `--yes` (a pipe or a cron job asks nothing)."""
    if not (getattr(args, "dry_run", False) or getattr(args, "yes", False) or sys.stdin.isatty()):
        print("refusing: this stops the gateway and there is no terminal to ask; pass --yes to run it "
              "unattended (nothing was stopped)")
        return 1
    rc = doctor(force=args.force, dry_run=args.dry_run, cleanup_sessions=args.cleanup_sessions,
                even_if_busy=getattr(args, "even_if_busy", False),
                drain_timeout=args.drain_timeout, health_timeout=args.health_timeout)
    if rc == EXIT_REFUSED_BUSY:
        print("nothing was stopped. Pass --even-if-busy to run it anyway (it aborts the runs in flight).")
    return rc


def cmd_pressure(args: argparse.Namespace, runner: Runner | None = None, **kw) -> int:
    """`ai-resources openclaw pressure [--json] [--confirm-seconds N]`: read-only classification.

    Exit 0 whatever the classification (the caller reads it); 1 only for an internal error. It
    runs `systemctl show`, `openclaw health` and `free -b` and reads cgroup and /proc files.
    """
    knobs = restart_knobs()
    confirm = args.confirm_seconds if args.confirm_seconds is not None else knob_number(knobs, "OPENCLAW_HEALTH_CONFIRM_S")
    result = openclaw_pressure.evaluate(
        runner=runner or default_runner, openclaw_bin=resolve_openclaw_bin(), confirm_seconds=confirm,
        env=systemd_env(),
        pressure_pct=knob_number(knobs, "OPENCLAW_RESTART_PRESSURE_PCT"),
        hard_pct=knob_number(knobs, "OPENCLAW_RESTART_HARD_PCT"),
        frozen_min_s=knob_number(knobs, "OPENCLAW_FROZEN_MIN_S"), **kw)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        ev = result["evidence"]
        print(f"{result['classification']}: memory {ev.get('memory_pct')}% of MemoryHigh, swap {ev.get('swap_pct')}%, "
              f"high events +{ev.get('high_events_delta')}, state {ev.get('proc_state')}"
              + (f" ({ev['reason']})" if ev.get("reason") else ""))
    return 0


def cmd_restart_report(args: argparse.Namespace, runner: Runner | None = None) -> int:
    """`ai-resources openclaw restart-report --since <epoch|iso> [--json]`: parse the journal (read-only)."""
    report = openclaw_pressure.read_report(args.since, runner or default_runner)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print("journal unavailable" if not report["available"] else
              ", ".join(f"{k}={v}" for k, v in report.items() if k != "available"))
    return 0 if report["available"] else 1


def cmd_busy(args: argparse.Namespace) -> int:
    """Exit 0 idle, 1 busy, 2 the probe could not tell (callers treat 2 as busy)."""
    n = gateway_busy_strict()
    if args.json:
        print(json.dumps({"busy": n, "probe_ok": n is not None}))
    else:
        print("unknown (the probe could not tell; treated as busy)" if n is None else f"{n} agent run(s) in flight")
    return 2 if n is None else (1 if n > 0 else 0)


# --- the uploader's own gcloud configuration (opt-in, 2.1.1) --------------------------------------------------------
#
# The uploader must not run as the agent identity the user manager exports (pitfall T43). The kit can create a
# NAMED gcloud configuration for it: operator account + impersonation of the backup service account, never
# activated, never with a credential_file_override, never touching another configuration. The IAM grant that lets
# the operator impersonate the service account is an infra step the kit documents and does not perform.

OFFBOX_CONFIG_NAME = "openclaw-backup"
OFFBOX_SA_DEFAULT = "k3s-backup@wildbit-iac.iam.gserviceaccount.com"
OFFBOX_PROTECTED_CONFIGS = ("default", "claude-ro")
_GCLOUD_UNSET = ("", "(unset)")


def _gcloud_value(runner: Runner, *args: str) -> str:
    rc, text = runner(["gcloud", *args], timeout=30)
    value = text.strip().splitlines()[-1].strip() if rc == 0 and text.strip() else ""
    return "" if value in _GCLOUD_UNSET else value


def _gcloud_configs(runner: Runner) -> "dict[str, bool] | None":
    """{configuration name: is_active}, None when gcloud cannot say."""
    rc, text = runner(["gcloud", "config", "configurations", "list", "--format=value(name,is_active)"], timeout=30)
    if rc != 0:
        return None
    out: dict[str, bool] = {}
    for line in text.splitlines():
        parts = line.split()
        if parts:
            out[parts[0]] = len(parts) > 1 and parts[1].lower() == "true"
    return out


# `\Z`, never `$`: `$` also matches before a trailing newline. The domain of the service account has the shape of
# a project id, so the project derived from it can never start with `-` or reach gcloud as a flag.
_OFFBOX_SA_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com\Z")
_OFFBOX_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*\Z")
_OFFBOX_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}\Z")
_OFFBOX_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]\Z")


def offbox_identity_value_problems(*, name: str, sa: str, account: str = "", project: str = "") -> list[str]:
    """Shape check of the values that end up in gcloud argv and in the sourced kit-host.env. An empty account
    or project means "not given yet" and is not checked here. Anything starting with `-` never matches."""
    checks = (("--name", name, _OFFBOX_NAME_RE, "a lowercase configuration name (letters, digits, dashes)"),
              ("--sa", sa, _OFFBOX_SA_RE, "a service account e-mail (NAME@PROJECT.iam.gserviceaccount.com)"),
              ("--account", account, _OFFBOX_ACCOUNT_RE, "an e-mail address"),
              ("--project", project, _OFFBOX_PROJECT_RE, "a GCP project id"))
    return [f"{flag} {value!r} is not {what}" for flag, value, rx, what in checks
            if (value or flag in ("--name", "--sa")) and not rx.match(value)]


def offbox_identity_account(runner: Runner) -> str:
    """The operator's own login: the active credentialed account, when it is not a service account."""
    acct = _gcloud_value(runner, "auth", "list", "--filter=status:ACTIVE", "--format=value(account)")
    return "" if acct.endswith(".gserviceaccount.com") else acct


def offbox_identity_plan(runner: Runner, *, name: str = OFFBOX_CONFIG_NAME, sa: str = OFFBOX_SA_DEFAULT,
                         account: str = "", project: str = "", created: "dict | None" = None) -> dict:
    """Read first, then list the exact gcloud commands that are still needed. Runs no write.

    {"name", "account", "sa", "project", "exists", "steps": [argv...], "problems": [...]}; `steps` is empty
    when the configuration already matches (idempotent), and `problems` non-empty means nothing may be applied.
    """
    created = _offbox_record(created)
    problems: list[str] = offbox_identity_value_problems(name=name, sa=sa, account=account, project=project)
    if problems:
        return {"name": name, "account": account, "sa": sa, "project": project, "exists": False, "steps": [],
                "problems": problems}
    project = project or (sa.split("@", 1)[1].split(".", 1)[0] if "@" in sa else "")
    if not _OFFBOX_PROJECT_RE.match(project):
        return {"name": name, "account": account, "sa": sa, "project": project, "exists": False, "steps": [],
                "problems": [f"the project {project!r} derived from --sa is not a GCP project id; pass --project"]}
    account = account or offbox_identity_account(runner)
    if account and not _OFFBOX_ACCOUNT_RE.match(account):
        problems.append(f"the active gcloud login {account!r} is not an e-mail address; pass --account")
    if not account:
        problems.append("no operator login found (`gcloud auth list` has no active user account); pass --account")
    if name in OFFBOX_PROTECTED_CONFIGS:
        problems.append(f"{name!r} is a configuration the kit never touches; pick another --name")
    configs = _gcloud_configs(runner)
    if configs is None:
        problems.append("gcloud could not list its configurations (is gcloud installed and signed in?)")
        configs = {}
    if created.get("name") and created["name"] != name:
        problems.append(f"the kit already created the gcloud configuration {created['name']!r}; remove it first with "
                        f"`ai-resources openclaw offbox-identity --remove`, then create {name!r}")
    exists = name in configs
    if exists and created.get("name") != name:
        problems.append(f"a gcloud configuration named {name!r} already exists and the kit did not create it; "
                        "pick another --name or remove it yourself")
    steps: list[list[str]] = []
    if not problems:
        if not exists:
            steps.append(["gcloud", "config", "configurations", "create", name, "--no-activate"])
        wanted = (("account", account), ("auth/impersonate_service_account", sa), ("core/project", project))
        for prop, want in wanted:
            if not want:
                continue
            have = _gcloud_value(runner, "config", "get", prop, f"--configuration={name}") if exists else ""
            if have != want:
                steps.append(["gcloud", "config", "set", prop, want, f"--configuration={name}"])
    return {"name": name, "account": account, "sa": sa, "project": project, "exists": exists, "steps": steps,
            "problems": problems}


_OFFBOX_PROP_KEYS = {"account": "account", "auth/impersonate_service_account": "impersonate",
                     "core/project": "project"}


def offbox_identity_apply(runner: Runner, plan: dict, *, out: Callable[[str], None] = print,
                          progress: "dict | None" = None) -> "dict | None":
    """Run `plan["steps"]`. The record to keep ({name, account, impersonate, project}) or None on a failure.

    `progress`, when given, is filled as the steps succeed: the moment `configurations create` returns it holds
    the name, and each `config set` adds its property. A caller that gets None back can still keep what was
    created, so a teardown or a rerun can remove or adopt the configuration (L5).

    The configuration is never activated and `credential_file_override` is never set: the plan has no such step."""
    if plan["problems"]:
        return None
    for argv in plan["steps"]:
        rc, text = runner(argv, timeout=60)
        if rc != 0:
            out(f"failed: {' '.join(argv)}: {text.strip()[-200:]}")
            return None
        if progress is not None:
            if argv[:4] == ["gcloud", "config", "configurations", "create"]:
                progress.update({"name": plan["name"], "account": "", "impersonate": "", "project": ""})
            elif argv[:3] == ["gcloud", "config", "set"] and argv[3] in _OFFBOX_PROP_KEYS:
                progress[_OFFBOX_PROP_KEYS[argv[3]]] = argv[4]
    return {"name": plan["name"], "account": plan["account"], "impersonate": plan["sa"],
            "project": plan["project"]}


def offbox_identity_remove(runner: Runner, record: dict, *, out: Callable[[str], None] = print) -> bool:
    """Delete the configuration the kit created, only while it still matches what the kit set. True when done
    (or already gone); False when it was left alone."""
    name = record.get("name", "")
    # A recorded name is state the operator can edit: only a name the kit itself would create is ever deleted.
    if not isinstance(name, str) or not _OFFBOX_NAME_RE.match(name) or name in OFFBOX_PROTECTED_CONFIGS:
        if name:
            out(f"the recorded gcloud configuration name {name!r} is not one the kit creates; it was left as it is")
        return False
    configs = _gcloud_configs(runner)
    if configs is None:
        out("gcloud could not list its configurations; the uploader configuration was left as it is")
        return False
    if name not in configs:
        return True
    if configs[name]:
        out(f"the gcloud configuration {name!r} is ACTIVE now; it was left as it is")
        return False
    for prop, key in (("account", "account"), ("auth/impersonate_service_account", "impersonate"),
                      ("core/project", "project")):
        have = _gcloud_value(runner, "config", "get", prop, f"--configuration={name}")
        if have != (record.get(key) or ""):
            out(f"the gcloud configuration {name!r} changed since the kit created it ({prop}); it was left as it is")
            return False
    rc, text = runner(["gcloud", "config", "configurations", "delete", name, "--quiet"], timeout=60)
    if rc != 0:
        out(f"could not delete the gcloud configuration {name!r}: {text.strip()[-200:]}")
        return False
    return True


OFFBOX_INFRA_NOTE = ("Infra step, not done by the kit: the operator account needs roles/iam.serviceAccountTokenCreator "
                     "on {sa} (apply the org-iac IAM draft). Then test with:\n"
                     "  CLOUDSDK_ACTIVE_CONFIG_NAME={name} gcloud storage ls gs://<bucket>/daily/")


def _offbox_record(raw) -> dict:
    """The recorded off-box configuration as a dict; a hand-edited state that holds anything else is no record."""
    return dict(raw) if isinstance(raw, dict) else {}


def cmd_offbox_identity(args: argparse.Namespace) -> int:
    """`offbox-identity`: the whole load -> modify -> save runs under the setup-state lock, so two runs cannot
    each create a configuration and record only the last one."""
    from .setup import state as setup_state
    if getattr(args, "dry_run", False):
        return _cmd_offbox_identity(args)        # a dry run writes nothing, so it takes (and creates) no lock
    try:
        with setup_state.exclusive():
            if setup_state.save_would_refuse():
                # before any gcloud call: a configuration created now could never be recorded
                print(setup_state.refusal_text())
                return 1
            try:
                return _cmd_offbox_identity(args)
            except setup_state.StateCorrupt:
                print(setup_state.refusal_text())
                return 1
    except setup_state.StateLockBusy:
        print("refusing: another offbox-identity run is in progress (the setup state is locked); "
              "wait for it to finish and run this again")
        return 1
    except setup_state.StateLockUnusable as e:
        print(f"refusing: the setup-state lock file cannot be used ({e}); remove it or fix its permissions "
              "and run this again")
        return 1


def _cmd_offbox_identity(args: argparse.Namespace) -> int:
    from .setup import state as setup_state
    s = setup_state.load()
    o = s.openclaw
    runner = default_runner
    if not getattr(args, "dry_run", False):
        problem = host_env_problem()
        if problem:
            # before any gcloud call: nothing was created, nothing is half done
            print(f"refusing: {problem}; nothing was created. Fix it, then run this again.")
            return 1
    if getattr(args, "remove", False):
        record = _offbox_record(o.host_offbox_config_created)
        if not record:
            print("the kit created no gcloud configuration here; nothing to remove")
            return 0
        wanted = getattr(args, "name", None)
        if wanted and wanted != record.get("name"):
            print(f"refusing: the recorded configuration is {record.get('name')}; run --remove without --name "
                  "or with that name")
            return 1
        if getattr(args, "dry_run", False):
            print(f"dry run -- would delete the gcloud configuration {record['name']!r} if it still matches")
            return 0
        if offbox_identity_remove(runner, record):
            o.host_offbox_config_created = {}
            _forget_offbox_knobs(o, record)
            setup_state.save(s)
            print(f"removed the gcloud configuration {record['name']!r}")
            return 0
        return 1
    name = getattr(args, "name", None) or OFFBOX_CONFIG_NAME
    bad = offbox_identity_value_problems(name=name, sa=args.sa, account=args.account, project=args.project)
    if bad:
        for problem in bad:
            print(f"refusing: {problem}")
        return 1
    plan = offbox_identity_plan(runner, name=name, sa=args.sa, account=args.account, project=args.project,
                                created=_offbox_record(o.host_offbox_config_created))
    for problem in plan["problems"]:
        print(f"refusing: {problem}")
    if plan["problems"]:
        return 1
    if plan["steps"]:
        verb = "running" if getattr(args, "yes", False) and not getattr(args, "dry_run", False) else "would run"
        print(f"{verb}, in order (the configuration is never activated; no credential file, no IAM):")
        for argv in plan["steps"]:
            print("  " + shlex.join(argv))
    else:
        print(f"the gcloud configuration {plan['name']!r} already matches; nothing to run")
    print(OFFBOX_INFRA_NOTE.format(sa=plan["sa"], name=plan["name"]))
    if getattr(args, "dry_run", False):
        return 0
    if plan["steps"] and not getattr(args, "yes", False):
        answer = input("Apply these commands? [y/N] ").strip().lower() if sys.stdin.isatty() else ""
        if answer not in ("y", "yes"):
            print("nothing applied (use --yes to apply without asking)")
            return 1
    progress: dict = _offbox_record(o.host_offbox_config_created)
    record = offbox_identity_apply(runner, plan, progress=progress) if plan["steps"] else (
        _offbox_record(o.host_offbox_config_created) or {
            "name": plan["name"], "account": plan["account"], "impersonate": plan["sa"],
            "project": plan["project"]})
    if record is None:
        if progress.get("name") == plan["name"] and progress != _offbox_record(o.host_offbox_config_created):
            # Created, then a later step failed: keep what exists so `--remove` or a rerun can deal with it.
            o.host_offbox_config_created = progress
            setup_state.save(s)
            print(f"the gcloud configuration {plan['name']!r} was created but not finished; "
                  "re-run to complete it, or use --remove")
        return 1
    if plan["steps"] and (not plan["exists"]
                          or _offbox_record(o.host_offbox_config_created).get("name") == plan["name"]):
        o.host_offbox_config_created = record
    written = _write_offbox_knobs(o, plan["name"], plan["sa"])
    setup_state.save(s)
    print("kit-host.env: " + (", ".join(written) if written else "knobs already set"))
    return 0


def _write_offbox_knobs(o, name: str, sa: str) -> list[str]:
    """Fill-only: a knob the operator already set is left alone. Records the previous value for teardown."""
    current = read_host_env()
    todo = {k: v for k, v in (("OPENCLAW_OFFBOX_GCLOUD_CONFIG", name), ("OPENCLAW_OFFBOX_IMPERSONATE_SA", sa))
            if not current.get(k)}
    for key in todo:
        o.host_env_previous.setdefault(key, None)
    if todo:
        write_host_env(todo)
    return sorted(todo)


def _forget_offbox_knobs(o, record: dict) -> None:
    """Remove the knobs only when they still hold exactly what the kit wrote."""
    current = read_host_env()
    todo: dict[str, None] = {}
    if current.get("OPENCLAW_OFFBOX_GCLOUD_CONFIG") == record.get("name"):
        todo["OPENCLAW_OFFBOX_GCLOUD_CONFIG"] = None
    if current.get("OPENCLAW_OFFBOX_IMPERSONATE_SA") == record.get("impersonate"):
        todo["OPENCLAW_OFFBOX_IMPERSONATE_SA"] = None
    if todo:
        write_host_env(dict(todo))
    for key in todo:
        o.host_env_previous.pop(key, None)


def cmd_dedupe_agents_md(args: argparse.Namespace) -> int:
    """`dedupe-agents-md [--apply] [PATH...]`: remove the hand-written copy of the kit's long-running section.

    Never silent and never unattended: a dry run (the diff) unless `--apply`, and `--apply` asks first. A copy that
    differs from the kit's text is shown and left alone."""
    from .setup import ui
    from .setup.cockpits import _shared
    from .setup.cockpits import openclaw as cockpit

    if args.paths:
        targets = [Path(p).expanduser() for p in args.paths]
    else:
        doc = cockpit.read_config(cockpit._verify_config_path()) or {}
        targets = [ws / "AGENTS.md" for ws in cockpit._workspace_targets(doc) if (ws / "AGENTS.md").is_file()]
    if not targets:
        print("no AGENTS.md file to look at")
        return 0
    rc = 0
    pending = []
    for path in targets:
        res = _shared.dedupe_long_running(path, apply=False)
        if res["status"] == "none":
            print(f"{path}: no hand-written copy of the kit section")
        elif res["status"] == "symlink":
            print(f"{path}: is a symlink to {res['target']}; left alone. Run the verb on the real file.")
            rc = 1
        elif res["status"] == "differs":
            print(f"{path}: the hand-written section differs from the kit's text; left alone. How it differs:")
            print(res["diff"], end="")
            rc = 1
        else:
            print(f"{path}: an identical hand-written copy would be removed:")
            print(res["diff"], end="")
            pending.append(path)
    if not pending:
        return rc
    if not args.apply:
        print("dry run: nothing changed. Re-run with --apply to remove the identical copies (a backup is written first).")
        return rc
    try:
        # A real terminal is required: a piped `y` is not a person looking at the diff (T2).
        confirmed = not ui.is_non_interactive() and ui.stdin_is_a_terminal() and ui.confirm(
            f"Remove the hand-written copy from {len(pending)} file(s)? A .bak-<timestamp> file is written first.",
            default=False)
    except EOFError:                 # stdin is not a terminal: one line, not a questionary traceback (G3)
        confirmed = False
    if not confirmed:
        print("nothing changed (--apply needs an interactive confirm)")
        return 1
    for path in pending:
        try:
            res = _shared.dedupe_long_running(path, apply=True)
        except OSError as e:
            print(f"{path}: could not write the backup ({e}); nothing written")
            rc = 1
            continue
        if res["status"] == "write-failed":
            print(f"{path}: could not write {path.name}; the backup is at {res['backup']} ({res['error']})")
            rc = 1
            continue
        if res["status"] != "removed":
            print(f"{path}: file changed, nothing written")
            rc = 1
            continue
        print(f"{path}: removed; backup {res['backup']}")
    return rc


def cmd_apply_pending(args: argparse.Namespace) -> int:
    """`apply-pending` (and `--discard-watch-restores`): load -> modify -> save under the setup-state lock."""
    from .setup import state as setup_state

    def run() -> int:
        if setup_state.save_would_refuse():
            print(setup_state.refusal_text())
            return 1
        try:
            return _cmd_apply_pending(args)
        except setup_state.StateCorrupt:
            print(setup_state.refusal_text())
            return 1
    return setup_state.locked_run(run)


def _cmd_apply_pending(args: argparse.Namespace) -> int:
    from .setup import state as setup_state
    from .setup.cockpits import _openclaw_host as section
    s = setup_state.load()
    if getattr(args, "discard_watch_restores", False):
        import copy
        before = copy.deepcopy(s.openclaw)
        rc = section.discard_watch_restores(s, assume_yes=args.yes)
        if s.openclaw != before:          # a dry run or a refusal changes nothing, so it must not bump `last_run`
            setup_state.save(s)
        return rc
    import copy
    before = copy.deepcopy(s.openclaw)
    rc = section.apply_pending(s, assume_yes=args.yes)
    if s.openclaw != before:              # a refusal, a decline or nothing pending changes nothing: no `last_run` bump
        setup_state.save(s)
    return rc


def cmd_agent_new(args: argparse.Namespace) -> int:
    try:
        res = agent_new(args.agent_id, args.workspace, kind=args.kind or None, force=args.force,
                        model=args.model, register=not args.no_register)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}")
        return 2
    print(f"workspace {res['workspace']} ({res['kind']}): AGENTS.md {res['agents_md']}")
    if res["excluded"]:
        print("added the OpenClaw files to .git/info/exclude")
    if res["registered"]:
        print(f"agent {args.agent_id}: {res['registered']}")
    print(res["reminder"])
    return 1 if str(res["registered"]).startswith("failed") else 0


def cmd_status(args: argparse.Namespace, runner: Runner | None = None) -> int:
    kw = {"runner": runner} if runner is not None else {}
    report = collect_status(run_doctor=not getattr(args, "no_doctor", False),
                            selection=recorded_selection(), **kw)
    print(render_status(report))
    # Journal findings (egress TLS refusals, a failed close that lost completions): outside the sections too.
    # Only for a real report, so a stubbed status never reads the host journal.
    if report.get("unit"):
        for level, message, remedy in journal_findings(runner or default_runner, report.get("stability")):
            print(f"{level.upper()}  {message}\n      -> {remedy}")
    # Outside the ten sections on purpose: it is not host state but a gap setup named and left.
    try:
        from .setup import state
        pending = pending_restart_report(state.load().openclaw.gateway_restart_pending)
    except Exception:  # noqa: BLE001 - an unreadable setup state must not hide the report above
        pending = ""
    if pending:
        print(pending)
    # Same place, same reason: restart-required config setup did not apply, and the last post-setup watch.
    for line in pending_lines(recorded_pending()):
        print(line)
    return 0  # status never repairs and never fails the shell: it reports


def _bootstrap_confirm(assume_yes: bool) -> Callable[[str, str], bool]:
    """The per-step confirm of the `bootstrap` verb: Yes only with `--yes`; otherwise each change is asked on a
    terminal with the default No, and an unattended run changes nothing (kit 2.2b, 3b)."""
    if assume_yes:
        return lambda step, what: True

    def ask(step: str, what: str) -> bool:
        from .setup import ui
        if ui.is_non_interactive() or not ui.stdin_is_a_terminal():
            return False
        try:
            return bool(ui.confirm(f"{what}?", default=False))
        except EOFError:
            return False
    return ask


def cmd_bootstrap(args: argparse.Namespace) -> int:
    try:
        results, _ctx = bootstrap(dry_run=args.dry_run, only=args.only or None, upgrade=args.upgrade,
                                  memory_high=args.memory_high,
                                  confirm=_bootstrap_confirm(getattr(args, "yes", False)))
    except ValueError as e:
        print(f"error: {e}")
        return 2
    return 0 if all(r.status in OK_STATUSES for r in results) else 1


def add_subparser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser("openclaw", help="OpenClaw host operations (units, doctor, bootstrap, status)")
    verbs = p.add_subparsers(dest="openclaw_verb", metavar="VERB")

    p_units = verbs.add_parser("install-units", help="Render and install the openclaw-* systemd units")
    p_units.add_argument("--dest", default="", help="Unit directory (default: ~/.config/systemd/user)")
    p_units.add_argument("--dry-run", action="store_true", help="Show what would change; write nothing")
    p_units.add_argument("--render-only", action="store_true", help="Print the rendered units; write nothing")
    p_units.add_argument("--enable", action="store_true", help="Also enable and start the timers")
    p_units.set_defaults(func=cmd_install_units)

    p_gr = verbs.add_parser("render-gitops-backups",
                            help="Render the off-box backup manifests (bucket, uploader, guard, alerts) for a GitOps repo")
    p_gr.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="A marker value (repeatable)")
    p_gr.add_argument("--out", default="", help="Write the four files here (default: print them)")
    p_gr.add_argument("--list-markers", action="store_true", help="Print the markers the templates need and stop")
    p_gr.set_defaults(func=cmd_render_gitops_backups)

    p_doc = verbs.add_parser("doctor", help="Run `openclaw doctor --fix` safely: drain, fix, restart, verify")
    p_doc.add_argument("--force", action="store_true", help="Proceed although watchdog.off already exists")
    p_doc.add_argument("--dry-run", action="store_true", help="Print the sequence; touch nothing")
    p_doc.add_argument("--yes", action="store_true",
                       help="Run without a terminal (it stops the gateway; with a terminal no flag is needed)")
    p_doc.add_argument("--even-if-busy", action="store_true",
                       help="Proceed although agent runs are in flight (or the probe cannot tell); the stop aborts them")
    p_doc.add_argument("--drain-timeout", type=float, default=90, help="Seconds to wait for the cgroup to drain")
    p_doc.add_argument("--health-timeout", type=float, default=120, help="Seconds to wait for the gateway to answer")
    p_doc.add_argument("--cleanup-sessions", action="store_true",
                       help="Also run `openclaw sessions cleanup --all-agents` inside the drained window")
    p_doc.set_defaults(func=cmd_doctor)

    p_pr = verbs.add_parser("pressure", help="Classify the gateway's memory pressure (read-only; ADR-0004)")
    p_pr.add_argument("--json", action="store_true", help="Print the classification and its evidence as JSON")
    p_pr.add_argument("--confirm-seconds", type=float, default=None,
                      help="Seconds between the two samples (default OPENCLAW_HEALTH_CONFIRM_S, 60)")
    p_pr.set_defaults(func=cmd_pressure)

    p_rr = verbs.add_parser("restart-report", help="Count the restart-recovery markers in the gateway journal (read-only)")
    p_rr.add_argument("--since", required=True, help="Epoch seconds or an ISO timestamp")
    p_rr.add_argument("--json", action="store_true", help="Print the counts as JSON")
    p_rr.set_defaults(func=cmd_restart_report)

    p_gr = verbs.add_parser("graceful-restart",
                            help="Restart the gateway gracefully under confirmed memory pressure (ADR-0004)")
    p_gr.add_argument("--reason", default="manual",
                      help="`memory` (the timer): the mode and pressure gates apply. `manual` (the default; a "
                           "supervised run): skips the mode and pressure gates, so NOTHING is measured and the "
                           "classification stays null; it is refused outside the restart window unless "
                           "--classification hard is given. Run `ai-resources openclaw pressure --json` first.")
    p_gr.add_argument("--dry-run", action="store_true",
                      help="Evaluate every gate and print the plan; change nothing (no state, marker, snapshot)")
    p_gr.add_argument("--classification", choices=("pressure", "hard"), default=None,
                      help="The caller's confirmed classification (the timer passes it). Omitted with `--reason "
                           "memory`, it is measured now. Omitted with `--reason manual`, it is NOT measured "
                           "(it stays null and no pressure gate runs). `hard` lets the run through outside the "
                           "window; confirm it with `ai-resources openclaw pressure --json` first")
    p_gr.add_argument("--json", action="store_true", help="Print the summary as JSON")
    p_gr.set_defaults(func=cmd_graceful_restart)

    p_busy = verbs.add_parser("busy", help="How many agent runs are in flight (exit 0 idle, 1 busy, 2 unknown)")
    p_busy.add_argument("--json", action="store_true", help="Print {busy, probe_ok} as JSON")
    p_busy.set_defaults(func=cmd_busy)

    p_ap = verbs.add_parser("apply-pending",
                            help="Apply the restart-required config setup deferred, inside a drained window (only when idle)")
    p_ap.add_argument("--yes", action="store_true", help="Do not ask for confirmation (it still refuses while busy)")
    p_ap.add_argument("--discard-watch-restores", action="store_true",
                      help="List the restore entries the post-setup watch queued (a dry run); with --yes or a "
                           "confirmation, drop them. Applies nothing")
    p_ap.set_defaults(func=cmd_apply_pending)

    p_dd = verbs.add_parser("dedupe-agents-md",
                            help="Show (and with --apply and a confirmation, remove) the hand-written copy of the "
                                 "kit's long-running section that `verify` flags in an AGENTS.md")
    p_dd.add_argument("paths", nargs="*", help="AGENTS.md files (default: the ones `verify` looks at)")
    p_dd.add_argument("--apply", action="store_true", help="Remove identical copies after a confirmation and a backup")
    p_dd.set_defaults(func=cmd_dedupe_agents_md)

    p_oi = verbs.add_parser("offbox-identity",
                            help="Create (or with --remove, delete) the named gcloud configuration the off-box "
                                 "uploader uses; never activated, no credential file, no IAM")
    p_oi.add_argument("--dry-run", action="store_true", help="Print the exact commands; change nothing")
    p_oi.add_argument("--remove", action="store_true", help="Delete the configuration the kit created, if unchanged")
    p_oi.add_argument("--yes", action="store_true", help="Apply without asking")
    p_oi.add_argument("--name", default=None, help=f"Configuration name (default {OFFBOX_CONFIG_NAME}); "
                      "--remove removes the recorded one and refuses a different name")
    p_oi.add_argument("--sa", default=OFFBOX_SA_DEFAULT, help="Service account to impersonate")
    p_oi.add_argument("--account", default="", help="Operator account (default: the active gcloud login)")
    p_oi.add_argument("--project", default="", help="core/project (default: the service account's project)")
    p_oi.set_defaults(func=cmd_offbox_identity)

    p_cw = verbs.add_parser("config-watch",
                            help="(started by setup) watch the gateway after a setup run; revert the run if it stops answering")
    p_cw.add_argument("--run-id", required=True, help="The setup run this watch belongs to")
    p_cw.add_argument("--window", type=float, default=WATCH_WINDOW, help="Seconds to watch (default 900)")
    p_cw.add_argument("--interval", type=float, default=WATCH_INTERVAL, help="Seconds between checks (default 30)")
    p_cw.set_defaults(func=cmd_config_watch)

    p_st = verbs.add_parser("status", help="One-screen host status (never repairs)")
    p_st.add_argument("--no-doctor", action="store_true",
                      help="Skip the `openclaw doctor` probe (the slow one; answers in about a second)")
    p_st.set_defaults(func=cmd_status)

    p_bs = verbs.add_parser("bootstrap", help="Bring a host to the documented state, idempotently")
    p_bs.add_argument("--dry-run", action="store_true", help="Report what would change; touch nothing")
    p_bs.add_argument("--only", default="", choices=["", *BOOTSTRAP_STEPS], help="Run a single step")
    p_bs.add_argument("--upgrade", action="store_true", help="Reinstall the latest openclaw even if one is present")
    p_bs.add_argument("--memory-high", default="12G", help="MemoryHigh for the gateway unit (default 12G)")
    p_bs.add_argument("--yes", action="store_true",
                      help="Make every change without asking (otherwise each one asks on a terminal, default No, "
                           "and an unattended run changes nothing)")
    p_bs.set_defaults(func=cmd_bootstrap)

    p_new = verbs.add_parser("agent-new", help="Give an OpenClaw agent a workspace with the right AGENTS.md")
    p_new.add_argument("agent_id")
    p_new.add_argument("workspace")
    p_new.add_argument("--kind", choices=sorted(AGENTS_TEMPLATES), default="",
                       help="Template (default: detected from the workspace)")
    p_new.add_argument("--force", action="store_true", help="Overwrite an existing AGENTS.md")
    p_new.add_argument("--model", default="", help="Model ref for the new agent")
    p_new.add_argument("--no-register", action="store_true", help="Write the files only; do not run `openclaw agents add`")
    p_new.set_defaults(func=cmd_agent_new)

    p.set_defaults(func=lambda a: (p.print_help(), 1)[1])
