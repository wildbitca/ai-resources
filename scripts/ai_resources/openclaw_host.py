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
  started again (T01). Every other verb reports "restart needed" and stops.

Every function that touches the system takes an injectable `runner`, so the tests drive the
real state machines against a fake and nothing in the suite can reach a live unit.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import repo_root

# (return code, combined output). 127 means the binary is missing.
Runner = Callable[..., "tuple[int, str]"]

BREW_OPENCLAW_FALLBACKS = ("/home/linuxbrew/.linuxbrew/bin/openclaw", "/opt/homebrew/bin/openclaw",
                           "/usr/local/bin/openclaw")
HOST_ENV_PATH = Path.home() / ".openclaw" / "kit-host.env"
WATCHDOG_OFF = Path.home() / ".openclaw" / "watchdog.off"
GATEWAY_UNIT = "openclaw-gateway.service"


def default_runner(argv: list[str], *, env: dict | None = None, timeout: float | None = 120,
                   input: str | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=input,
                           env=env, cwd=str(Path.home()))
    except FileNotFoundError:
        return 127, f"{argv[0]} not found"
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


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


_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ENV_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_@%+=:,./~ -]*$")


def _check_env_pair(key: str, value: str) -> None:
    if not _ENV_KEY.match(key):
        raise ValueError(f"bad host env key: {key!r}")
    if not _ENV_SAFE_VALUE.match(value):
        # The file is sourced by bash: refuse anything that could be code.
        raise ValueError(f"unsafe characters in the value of {key}: {value!r}")


def write_host_env(values: dict[str, str | None], path: Path | None = None) -> bool:
    """Set (or, for None, remove) the given keys, keeping every other line as it is.

    The file is created with the shipped example's header on first write. Returns True when it
    changed, so a second run with the same answers is a byte-level no-op.
    """
    path = path or HOST_ENV_PATH
    for key, value in values.items():
        if value is not None:
            _check_env_pair(key, value)
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
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, path)
    return True


# --- systemd units -----------------------------------------------------------------------------

TEMPLATES_DIR_NAME = Path("templates") / "systemd"
UNIT_NAMES = (
    "openclaw-backup@.service",
    "openclaw-backup-daily.timer",
    "openclaw-backup-weekly.timer",
    "openclaw-backup-monthly.timer",
    "openclaw-maintenance.service",
    "openclaw-maintenance.timer",
    "openclaw-watchdog.service",
    "openclaw-watchdog.timer",
    "openclaw-verify.service",
    "openclaw-verify.timer",
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


def install_units(dest: Path | None = None, *, markers: dict[str, str] | None = None,
                  dry_run: bool = False, enable: bool = False, runner: Runner = default_runner,
                  templates_dir: Path | None = None) -> dict:
    """Write all ten units at once, so the host never runs a mix of old and new ones.

    Returns {"changed": [...], "unchanged": [...], "reloaded": bool, "enabled": [...]}.
    Never touches the gateway unit. A second run with nothing to change writes nothing and
    reloads nothing.
    """
    dest = dest or user_unit_dir()
    rendered = render_units(markers, templates_dir)
    changed, unchanged = [], []
    for name, text in rendered.items():
        target = dest / name
        try:
            same = target.read_text(encoding="utf-8") == text
        except OSError:
            same = False
        (unchanged if same else changed).append(name)
    result = {"changed": changed, "unchanged": unchanged, "reloaded": False, "enabled": []}
    if dry_run:
        return result
    if changed:
        dest.mkdir(parents=True, exist_ok=True)
        for name in changed:
            tmp = dest / f".{name}.tmp"
            tmp.write_text(rendered[name], encoding="utf-8")
            os.replace(tmp, dest / name)
        rc, _out = runner(["systemctl", "--user", "daemon-reload"], env=systemd_env())
        result["reloaded"] = rc == 0
    if enable:
        for timer in TIMER_NAMES:
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


class Marker:
    """The `watchdog.off` file: while it exists the watchdog leaves the gateway alone."""

    def __init__(self, path: Path | None = None):
        self.path = path or WATCHDOG_OFF

    def exists(self) -> bool:
        return self.path.exists()

    def touch(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()

    def remove(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def doctor(runner: Runner = default_runner, *, force: bool = False, dry_run: bool = False,
           cleanup_sessions: bool = False, drain_timeout: float = 90, drain_interval: float = 3,
           health_timeout: float = 120, health_interval: float = 5,
           sleep: Callable[[float], None] = time.sleep, marker: Marker | None = None,
           openclaw_bin: str | None = None, out: Callable[[str], None] = print) -> int:
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
    not come back.
    """
    marker = marker or Marker()
    env = systemd_env()
    oc = openclaw_bin or resolve_openclaw_bin()
    sysctl = ["systemctl", "--user"]

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

    if marker.exists() and not force:
        out(f"refusing: {marker.path} exists, so another maintenance window is open. "
            "Pass --force if that window is dead.")
        return EXIT_REFUSED

    drained = doctor_ok = False
    code = EXIT_OK
    marker.touch()
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
            rc, text = runner([oc, "doctor", "--fix"], env=env, timeout=600)
            out(text)
            doctor_ok = rc == 0 and "Doctor complete" in text
            out(f"doctor={'ok' if doctor_ok else 'failed'}")
            if cleanup_sessions:
                _rc, cleaned = runner([oc, "sessions", "cleanup", "--all-agents"], env=env, timeout=300)
                out("sessions cleanup: " + " ".join(cleaned.splitlines()[-2:]))
        else:
            out(f"doctor=skipped (the cgroup did not drain in {drain_timeout:g}s)")
    finally:
        marker.remove()
        runner(sysctl + ["start", GATEWAY_UNIT], env=env, timeout=120)
        healthy = False
        for _ in range(max(1, int(health_timeout // health_interval))):
            sleep(health_interval)
            rc, _text = runner([oc, "health"], env=env, timeout=60)
            if rc == 0:
                healthy = True
                break
        out(f"healthy={'yes' if healthy else 'no'}")
        if not healthy:
            code = EXIT_NOT_HEALTHY
    if code:
        return code
    if not drained:
        return EXIT_NOT_DRAINED
    return EXIT_OK if doctor_ok else EXIT_DOCTOR_FAILED


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


def cmd_doctor(args: argparse.Namespace) -> int:
    return doctor(force=args.force, dry_run=args.dry_run, cleanup_sessions=args.cleanup_sessions,
                  drain_timeout=args.drain_timeout, health_timeout=args.health_timeout)


def add_subparser(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser("openclaw", help="OpenClaw host operations (units, doctor, bootstrap, status)")
    verbs = p.add_subparsers(dest="openclaw_verb", metavar="VERB")

    p_units = verbs.add_parser("install-units", help="Render and install the ten openclaw-* systemd units")
    p_units.add_argument("--dest", default="", help="Unit directory (default: ~/.config/systemd/user)")
    p_units.add_argument("--dry-run", action="store_true", help="Show what would change; write nothing")
    p_units.add_argument("--render-only", action="store_true", help="Print the rendered units; write nothing")
    p_units.add_argument("--enable", action="store_true", help="Also enable and start the timers")
    p_units.set_defaults(func=cmd_install_units)

    p_doc = verbs.add_parser("doctor", help="Run `openclaw doctor --fix` safely: drain, fix, restart, verify")
    p_doc.add_argument("--force", action="store_true", help="Proceed although watchdog.off already exists")
    p_doc.add_argument("--dry-run", action="store_true", help="Print the sequence; touch nothing")
    p_doc.add_argument("--drain-timeout", type=float, default=90, help="Seconds to wait for the cgroup to drain")
    p_doc.add_argument("--health-timeout", type=float, default=120, help="Seconds to wait for the gateway to answer")
    p_doc.add_argument("--cleanup-sessions", action="store_true",
                       help="Also run `openclaw sessions cleanup --all-agents` inside the drained window")
    p_doc.set_defaults(func=cmd_doctor)

    p.set_defaults(func=lambda a: (p.print_help(), 1)[1])
