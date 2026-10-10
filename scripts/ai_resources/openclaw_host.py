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
import subprocess
import time
from pathlib import Path
from typing import Callable, NamedTuple

from . import model_pins, openclaw_reload_rules, repo_root

# (return code, combined output). 127 means the binary is missing.
Runner = Callable[..., "tuple[int, str]"]

BREW_OPENCLAW_FALLBACKS = ("/home/linuxbrew/.linuxbrew/bin/openclaw", "/opt/homebrew/bin/openclaw",
                           "/usr/local/bin/openclaw")
HOST_ENV_PATH = Path.home() / ".openclaw" / "kit-host.env"
WATCHDOG_OFF = Path.home() / ".openclaw" / "watchdog.off"
GATEWAY_UNIT = "openclaw-gateway.service"


def default_runner(argv: list[str], *, env: dict | None = None, timeout: float | None = 120,
                   input: str | None = None, cwd: str | None = None) -> tuple[int, str]:
    """Run `argv`; (return code, combined output).

    127 is a missing binary and 124 a timeout (the coreutils `timeout` convention); any other
    OSError is 1. Every caller before the 124 split tested `rc == 0` only, so it is additive.
    """
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
    if script.is_file():
        found.append(script)
    rendered = rendered if rendered is not None else render_units()
    for name in HEALTH_UNITS:
        target = dest / name
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
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
            tmp = dest / f".{name}.tmp"
            tmp.write_text(rendered[name], encoding="utf-8")
            os.replace(tmp, dest / name)
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
           even_if_busy: bool = False, busy_probe: Callable[[], "int | None"] | None = None,
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
                          openclaw_bin=oc, out=out,
                          skip_message=f"doctor=skipped (the cgroup did not drain in {drain_timeout:g}s)")


EXIT_REFUSED_BUSY = 6     # require_idle was set and agent runs are in flight (or the probe failed)


def drained_window(action: Callable[[], bool], *, runner: Runner = default_runner, force: bool = False,
                   require_idle: bool = False, busy_probe: Callable[[], "int | None"] | None = None,
                   drain_timeout: float = 90, drain_interval: float = 3,
                   health_timeout: float = 120, health_interval: float = 5,
                   sleep: Callable[[float], None] = time.sleep, marker: Marker | None = None,
                   openclaw_bin: str | None = None, out: Callable[[str], None] = print,
                   skip_message: str = "") -> int:
    """The one maintenance window (T01), shared by `doctor` and the kit's restart-required config applies.

        touch watchdog.off  ->  stop  ->  poll until the cgroup drains  ->  action()
        ->  rm watchdog.off  ->  start  ->  poll `openclaw health`

    `action` runs ONLY once the unit drained, and returns True on success. The marker is removed
    and the gateway started again on EVERY exit path, `action` raising included. With
    `require_idle` the window is refused, before anything is stopped, when the strict busy probe
    reports runs in flight or cannot tell (ADR-0001: defer, never force).

    Exit codes: 0 ok; 2 refused (marker present); 3 not drained; 4 action failed; 5 gateway did
    not come back; 6 refused (busy).
    """
    marker = marker or Marker()
    env = systemd_env()
    oc = openclaw_bin or resolve_openclaw_bin()
    sysctl = ["systemctl", "--user"]

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
            action_ok = bool(action())
        elif skip_message:
            out(skip_message)
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

WATCH_UNIT_PREFIX = "openclaw-config-watch-"
WATCH_WINDOW = 600.0
WATCH_INTERVAL = 30.0
WATCH_FAILURES = 3
_TRANSIENT_STATES = frozenset({"activating", "deactivating", "reloading"})


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


def watch_config(run_id: str, *, runner: Runner = default_runner,
                 sleep: Callable[[float], None] = time.sleep, window: float = WATCH_WINDOW,
                 interval: float = WATCH_INTERVAL, failures: int = WATCH_FAILURES,
                 load: Callable[[], object] | None = None, save: Callable[[object], None] | None = None,
                 apply_patch: Callable[..., "tuple[bool, str]"] | None = None,
                 notify: Callable[[str], bool] | None = None, marker: Marker | None = None,
                 reload_mode: Callable[[], "str | None"] | None = None,
                 load_doc: Callable[[], "dict | None"] | None = None,
                 out: Callable[[str], None] = print, **window_kw: object) -> int:
    """Watch the gateway for `window` seconds after a setup run; revert that run's changes if it stops answering.

    Exit 0 whether or not it reverted (the unit's job is done); 1 when the revert itself failed.

    A tick that finds no `config_watch` for this run id (cancelled or superseded), `done` already
    set, or `watchdog.off` present (a maintenance window is open) ends the watch without a revert.
    A gateway that is activating, deactivating or reloading is neither a failure nor a success: a
    restart by OpenClaw, the watchdog or a kit window is in progress. After `failures` consecutive
    failed `openclaw health` calls everything the run recorded is reverted: hot keys at once with no
    restart; restart-required keys only inside the kit's drained window (a blind `config patch` of
    such a key would make OpenClaw force a restart over live runs after 300 s).
    """
    from .setup import state as setup_state
    from .setup.cockpits import openclaw as cockpit

    load = load or setup_state.load
    save = save or setup_state.save
    apply_patch = apply_patch or cockpit.apply_patch
    notify = notify or (lambda msg: notify_owner(msg, runner))
    reload_mode = reload_mode or cockpit._live_reload_mode
    marker = marker or Marker()
    oc = resolve_openclaw_bin()
    ticks = min(21, int(window // interval) + 1)
    bad = 0
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
        rc, _text = runner([oc, "health"], timeout=45)
        if rc == 0:
            bad = 0
            continue
        bad += 1
        if bad >= failures:
            if cw.get("baseline_healthy") is False:
                # The gateway was already failing before this run wrote anything (E2): the writes are not the
                # cause, so reverting them would only add change. Say so once and stop.
                cw["done"] = True
                cw["result"] = {"hot": [], "restart": [], "problems": [], "skipped": "baseline-unhealthy"}
                save(s)
                message = ("OpenClaw is not answering `openclaw health`, but it was already failing before the "
                           "last `ai-resources setup` wrote anything, so the kit did not revert that run. "
                           "Check `ai-resources openclaw status`.")
                out(message)
                if not notify(message):
                    out("watch: no Telegram target configured or the send failed; the journal has the details")
                return 0
            return _revert_run(s, cw, runner=runner, sleep=sleep, save=save, apply_patch=apply_patch, notify=notify,
                               reload_mode=reload_mode, marker=marker, out=out, load_doc=load_doc, **window_kw)
    out("watch: the gateway stayed healthy; nothing to revert")
    return 0


def _revert_run(s, cw: dict, *, runner: Runner, sleep, save, apply_patch, notify, reload_mode, marker, out,
                load_doc=None, **window_kw) -> int:
    rr = openclaw_reload_rules

    changes = list(cw.get("changes") or [])
    patch, rp = restore_patch(changes, load_doc() if load_doc else None)
    version = rr.installed_openclaw_version(lambda argv, **kw: runner([resolve_openclaw_bin(), *argv[1:]], **kw))
    restart = rr.restart_paths(patch, openclaw_version=version, reload_mode=reload_mode())
    hot, res = rr.split_patch(patch, restart)
    hot_rp, res_rp = rr.split_replace_paths(rp, restart)
    hot_paths, res_paths = rr.leaf_paths(hot), rr.leaf_paths(res)
    problems: list[str] = []
    done_hot = done_res = False
    out("watch: the gateway stopped answering; reverting this run's config changes")
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
        if not written:
            problems.append(f"restart-required keys not reverted (drained window exit {code})")
            for ch in changes:
                if ".".join(restore_path_of(ch)) in res_paths:
                    s.openclaw.host_restart_pending.append(
                        {"op": "restore", "path": ".".join(ch["path"]), "change": ch, "source": "watch",
                         "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    cw["done"] = True
    cw["result"] = {"hot": hot_paths if done_hot else [], "restart": res_paths if done_res else [],
                    "problems": problems}
    save(s)
    parts = []
    if done_hot:
        parts.append("reverted: " + ", ".join(hot_paths))
    if done_res:
        parts.append("reverted in a drained window (restart-required): " + ", ".join(res_paths))
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

    return watch_config(args.run_id, window=args.window, interval=args.interval, load_doc=load_doc)


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
    cleaned = re.sub(r",(\s*[\]}])", r"\1", "".join(out))
    return json.loads(cleaned)


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
    The one policy exception is an agent still on a haiku primary (T29), which the wildcard entry
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
                if merged != current:
                    kept.append(dotted)        # `keep` blocks the union under this path
                continue
            wanted = merged
        if had and current == wanted:
            continue
        if had and isinstance(wanted, list) and isinstance(current, list):
            action = "filled"            # an add-only union: nothing the operator listed is lost
        elif not had:
            action = "filled"
        elif _override_hit(path, force_set) and not _override_hit(path, keep_set):
            action = "forced"
        elif path[:2] == ["agents", "entries"] and path[-2:] == ["model", "primary"] and _is_haiku(current):
            action = "forced"            # T29: no orchestrator may run haiku
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
        if (had and current == node) or (not had and node is None):
            return                    # a no-op write: nothing to record, nothing to put back (E1)
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
            if (ch["had"] and live_had and live == ch["previous"]) or (not ch["had"] and not live_had):
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
              confirm: Callable[[str, str], bool] = lambda step, what: True,
              home: Path | None = None, host_env_path: Path | None = None,
              sleep: Callable[[float], None] = time.sleep, health_wait: float = 60,
              unit_dir: Path | None = None, skip: tuple[str, ...] = (),
              out: Callable[[str], None] = print) -> tuple[list[StepResult], _Ctx]:
    """Eight idempotent steps in a fixed order; each checks first and reports satisfied or changed.

    `dry_run` runs the checks and prints what it WOULD do; nothing is touched. `only` runs one
    step. `confirm(step, description)` is asked before every change (the wizard passes a real
    prompt; the headless command passes yes). Never writes openclaw.json, never restarts the
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
STATUS_SECTIONS = ("unit", "boot", "listeners", "health", "timers", "backups", "off-box", "models", "identity", "stability", "doctor")


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


def pending_summary(o) -> dict:
    """What status and verify show about the restart-required config setup did not apply:
    {"paths": [...], "watch": {...}|None}, {} when there is nothing to say. Paths only, never values."""
    paths = sorted({str(e.get("path", "")) for e in (getattr(o, "host_restart_pending", None) or []) if e.get("path")})
    cw = getattr(o, "config_watch", None) or {}
    watch = None
    if cw.get("done") and cw.get("result"):
        r = cw["result"]
        watch = {"reverted": sorted(set(r.get("hot") or []) | set(r.get("restart") or [])),
                 "problems": list(r.get("problems") or []), "run_id": cw.get("run_id", "")}
    if not paths and not (watch and (watch["reverted"] or watch["problems"])):
        return {}
    return {"paths": paths, "watch": watch}


def recorded_pending() -> dict:
    """`pending_summary` of setup-state.yaml; {} when it is missing or broken (a status never fails on it)."""
    try:
        from .setup import state as setup_state
        return pending_summary(setup_state.load().openclaw)
    except Exception:  # noqa: BLE001
        return {}


PENDING_COMMAND = "ai-resources openclaw apply-pending"

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
    w = pending.get("watch")
    if w and w.get("reverted"):
        out.append("the post-setup watch reverted: " + ", ".join(w["reverted"]))
    if w and w.get("problems"):
        out.append("the post-setup watch could not finish its revert: " + "; ".join(w["problems"]))
    return out


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

    cfg_file = config_path or Path(os.environ.get("OPENCLAW_CONFIG_PATH") or home / ".openclaw" / "openclaw.json")
    try:
        cfg = json.loads(Path(cfg_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    port = str(((cfg.get("gateway") or {}).get("port")) or 18789)
    _rc, ss = runner(["ss", "-H", "-ltn"], env=env)
    listeners = [ln.split()[3] for ln in ss.splitlines() if len(ln.split()) >= 4 and ln.split()[3].endswith(f":{port}")]
    report["listeners"] = {"port": port, "addresses": listeners,
                           "wildcard": any(a.startswith(("0.0.0.0", "*", "[::]")) for a in listeners)}

    oc = resolve_openclaw_bin()   # one resolution for health and doctor: a login PATH may lack brew
    rc, _health = runner([oc, "health"], env=env)
    report["health"] = {"ok": rc == 0}

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


def render_status(report: dict) -> str:
    lines = ["OpenClaw host status", ""]
    u, b = report["unit"], report["boot"]
    lines.append(f"unit        {GATEWAY_UNIT}: {u['active']}, {u['enabled']}")
    lines.append(f"boot        linger: {b['linger']}")
    ls = report["listeners"]
    addr = ", ".join(ls["addresses"]) or "nothing listening"
    lines.append(f"listeners   port {ls['port']}: {addr}" + ("   ATTENTION: bound to all interfaces (T04)" if ls["wildcard"] else ""))
    lines.append(f"health      {'answers' if report['health']['ok'] else 'DOES NOT ANSWER'}")
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
    if not ob["configured"]:
        lines.append("off-box     not configured (set OPENCLAW_OFFBOX_LIST_CMD in kit-host.env)")
    elif "ok" not in ob:
        lines.append("off-box     no local daily to look for")
    else:
        lines.append("off-box     " + (f"newest daily {ob['name']} is present" if ob["present"]
                                       else f"newest daily {ob['name']} is MISSING off-box"
                                       if ob["ok"] else "the listing command failed"))
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


def cmd_doctor(args: argparse.Namespace) -> int:
    rc = doctor(force=args.force, dry_run=args.dry_run, cleanup_sessions=args.cleanup_sessions,
                even_if_busy=getattr(args, "even_if_busy", False),
                drain_timeout=args.drain_timeout, health_timeout=args.health_timeout)
    if rc == EXIT_REFUSED_BUSY:
        print("nothing was stopped. Pass --even-if-busy to run it anyway (it aborts the runs in flight).")
    return rc


def cmd_busy(args: argparse.Namespace) -> int:
    """Exit 0 idle, 1 busy, 2 the probe could not tell (callers treat 2 as busy)."""
    n = gateway_busy_strict()
    if args.json:
        print(json.dumps({"busy": n, "probe_ok": n is not None}))
    else:
        print("unknown (the probe could not tell; treated as busy)" if n is None else f"{n} agent run(s) in flight")
    return 2 if n is None else (1 if n > 0 else 0)


def cmd_apply_pending(args: argparse.Namespace) -> int:
    from .setup import state as setup_state
    from .setup.cockpits import _openclaw_host as section
    s = setup_state.load()
    rc = section.apply_pending(s, assume_yes=args.yes)
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


def cmd_status(args: argparse.Namespace) -> int:
    print(render_status(collect_status(run_doctor=not getattr(args, "no_doctor", False),
                                       selection=recorded_selection())))
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


def cmd_bootstrap(args: argparse.Namespace) -> int:
    try:
        results, _ctx = bootstrap(dry_run=args.dry_run, only=args.only or None, upgrade=args.upgrade,
                                  memory_high=args.memory_high)
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
    p_doc.add_argument("--even-if-busy", action="store_true",
                       help="Proceed although agent runs are in flight (or the probe cannot tell); the stop aborts them")
    p_doc.add_argument("--drain-timeout", type=float, default=90, help="Seconds to wait for the cgroup to drain")
    p_doc.add_argument("--health-timeout", type=float, default=120, help="Seconds to wait for the gateway to answer")
    p_doc.add_argument("--cleanup-sessions", action="store_true",
                       help="Also run `openclaw sessions cleanup --all-agents` inside the drained window")
    p_doc.set_defaults(func=cmd_doctor)

    p_busy = verbs.add_parser("busy", help="How many agent runs are in flight (exit 0 idle, 1 busy, 2 unknown)")
    p_busy.add_argument("--json", action="store_true", help="Print {busy, probe_ok} as JSON")
    p_busy.set_defaults(func=cmd_busy)

    p_ap = verbs.add_parser("apply-pending",
                            help="Apply the restart-required config setup deferred, inside a drained window (only when idle)")
    p_ap.add_argument("--yes", action="store_true", help="Do not ask for confirmation (it still refuses while busy)")
    p_ap.set_defaults(func=cmd_apply_pending)

    p_cw = verbs.add_parser("config-watch",
                            help="(started by setup) watch the gateway after a setup run; revert the run if it stops answering")
    p_cw.add_argument("--run-id", required=True, help="The setup run this watch belongs to")
    p_cw.add_argument("--window", type=float, default=WATCH_WINDOW, help="Seconds to watch (default 600)")
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
