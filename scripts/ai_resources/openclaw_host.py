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
import ipaddress
import json
import os
import re
import shlex
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
            primary = ((entry or {}).get("model") or {}).get("primary")
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


def build_host_patch(profile: dict, doc: dict, values: dict[str, str]) -> dict:
    """The minimal patch that makes `doc` canonical.

    Returns {"patch": {...}, "changes": [{"path": [...], "previous": v, "had": bool}],
             "replace_paths": [...], "skipped": [str]}.
    """
    values = dict(values)
    values.setdefault("HOME", str(Path.home()))
    tree = expand_wildcards(profile, doc)
    skipped: list[str] = []
    missing: set[str] = set()
    tree = _substitute(tree, values, missing)

    patch: dict = {}
    changes: list[dict] = []
    replace_paths: list[str] = []
    for path, wanted in _leaves(tree, []):
        dotted = ".".join(path)
        if _has_marker(wanted):
            skipped.append(f"{dotted}: needs " + ", ".join(sorted(missing)) + " (not set)")
            continue
        # A plugin's settings are only written when the plugin is already configured.
        if path[:2] == ["plugins", "entries"] and len(path) > 2:
            if not _get_path(doc, path[:3])[1]:
                skipped.append(f"{dotted}: plugin {path[2]} is not configured")
                continue
        if path == ["gateway", "controlUi", "github", "token"] and not _secret_env_available("GH_TOKEN"):
            skipped.append(f"{dotted}: GH_TOKEN is not available on this host (set it with `openclaw configure`)")
            continue
        current, had = _get_path(doc, path)
        if isinstance(wanted, list):
            wanted = _union(current, wanted)
        if had and current == wanted:
            continue
        cur = patch
        for k in path[:-1]:
            cur = cur.setdefault(k, {})
        cur[path[-1]] = wanted
        secret = is_secret_path(path)
        # A secret leaf records that it existed and nothing of its value.
        change = {"path": path, "previous": None if secret else (current if had else None), "had": had}
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
    return {"patch": patch, "changes": changes, "replace_paths": replace_paths, "skipped": skipped}


def restore_patch(changes: list[dict]) -> tuple[dict, list[str]]:
    """The patch that puts every changed leaf back as it was (absent leaves are deleted)."""
    patch: dict = {}
    replace_paths: list[str] = []
    for ch in changes:
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
TOPIC_REMINDER = ("A Telegram topic keeps its old context: send /new in the topic once so the agent "
                  "starts with this AGENTS.md (T19). Binding the topic is done with "
                  "`openclaw config set channels.telegram.groups[...]`; the kit never edits channels.")


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


def units_state(dest: Path | None = None, runner: Runner = default_runner) -> tuple[list[str], list[str]]:
    """(unit files that differ from their template, timers that are not enabled). Read-only."""
    plan = install_units(dest, dry_run=True, runner=runner)
    disabled = []
    for timer in TIMER_NAMES:
        rc, out = runner(["systemctl", "--user", "is-enabled", timer], env=systemd_env())
        if not (rc == 0 and out.strip() == "enabled"):
            disabled.append(timer)
    return plan["changed"], disabled


def _step_units(c: _Ctx) -> StepResult:
    changed, disabled = units_state(c.unit_dir, c.runner)
    plan = {"changed": changed}
    if not plan["changed"] and not disabled:
        return StepResult("units", "satisfied", f"{len(UNIT_NAMES)} units, {len(TIMER_NAMES)} timers")
    if c.dry_run:
        c.planned.append(f"units: install {len(plan['changed'])} unit(s), enable {len(disabled) or len(TIMER_NAMES)} timer(s)")
        return StepResult("units", "would-change", f"{len(plan['changed'])} to write, {len(disabled)} timers off")
    if not c.confirm("units", "install the openclaw-* units and enable their timers"):
        return StepResult("units", "declined")
    result = install_units(c.unit_dir, enable=True, runner=c.runner)
    c.commands.append(["ai-resources", "openclaw", "install-units", "--enable"])
    ok = len(result["enabled"]) == len(TIMER_NAMES)
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
    ("dashboard-conflict", re.compile(r'Plugin command "/dashboard" conflicts with an existing Telegram command', re.I)),
)
TIER_MAX_AGE_HOURS = {"daily": 36, "weekly": 8 * 24, "monthly": 35 * 24}
STATUS_SECTIONS = ("unit", "boot", "listeners", "health", "timers", "backups", "off-box", "models", "doctor")


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
    for title, entry in parse_doctor_entries(text):
        if any(pat.search(entry) for _, pat in DOCTOR_NOISE):
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


def collect_status(runner: Runner = default_runner, *, home: Path | None = None,
                   host_env_path: Path | None = None, config_path: Path | None = None,
                   now: float | None = None) -> dict:
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

    rc, _health = runner(["openclaw", "health"], env=env)
    report["health"] = {"ok": rc == 0}

    timers = []
    for name in TIMER_NAMES:
        _rc, nxt = sc("show", name, "-p", "NextElapseUSecRealtime", "--value")
        rc_e, en = sc("is-enabled", name)
        timers.append({"name": name, "enabled": rc_e == 0 and en.strip() == "enabled", "next": nxt.strip() or "-"})
    report["timers"] = timers

    base = Path(hostenv.get("OPENCLAW_BACKUP_DIR", "/srv/openclaw-backups"))
    tiers = backup_tiers(base, now)
    report["backups"] = tiers

    remote_cmd = hostenv.get("OPENCLAW_OFFBOX_LIST_CMD", "")
    offbox: dict = {"configured": bool(remote_cmd)}
    if remote_cmd and tiers["daily"].get("present"):
        rc, listing = runner(shlex.split(remote_cmd), env=env)
        offbox.update(ok=rc == 0, present=tiers["daily"]["name"] in listing if rc == 0 else False,
                      name=tiers["daily"]["name"])
    report["off-box"] = offbox

    default_model = (((cfg.get("agents") or {}).get("defaults") or {}).get("model") or {}).get("primary", "?")
    report["models"] = [{"agent": aid, "model": ((e or {}).get("model") or {}).get("primary") or f"{default_model} (default)"}
                        for aid, e in ((cfg.get("agents") or {}).get("entries") or {}).items()]

    rc, doctor = runner(["openclaw", "doctor", "--non-interactive"], env=env, timeout=120)
    noise, signal = filter_doctor_warnings(doctor) if rc == 0 or doctor else ([], [])
    report["doctor"] = {"ran": rc == 0, "noise": len(noise), "signal": signal}
    return report


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
        lines.append(f"  {m['agent']:<16} {m['model']}")
    d = report["doctor"]
    if not d["ran"]:
        lines.append("doctor      could not run")
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


def cmd_doctor(args: argparse.Namespace) -> int:
    return doctor(force=args.force, dry_run=args.dry_run, cleanup_sessions=args.cleanup_sessions,
                  drain_timeout=args.drain_timeout, health_timeout=args.health_timeout)


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
    print(render_status(collect_status()))
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

    p_st = verbs.add_parser("status", help="One-screen host status (never repairs)")
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
