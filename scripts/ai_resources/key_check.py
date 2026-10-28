"""Gemini and Anthropic key check: probe the gateway env key and the OpenClaw profile key, reconcile a rejected env key.

Two places can hold the same provider's key and they drift: `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` in
`~/.openclaw/gateway.systemd.env` (what the gateway process and its children see) and the `<provider>:default` auth
profile in OpenClaw's state store (what model turns use). The 2026-10-27 host had the env Gemini key answering HTTP 401
while the profile key worked, so half of the calls were unfunded.

Each key gets one cheap, free `models.list` call (no tokens are billed). The outcome per provider is one of:

- `env ok / profile ok`: both answer;
- `env rejected, profile ok` -> `mismatch fixed`: the env entry is rewritten from the profile key (a backup first, the
  file stays 0600, the value is never printed or logged) and a drained gateway restart is owed (the caller runs the
  existing `openclaw_reconcile.reconcile` with `RESTART_REASON`);
- `profile rejected`, `env absent`, `unknown` (no network): reported, nothing is written.

Key VALUES never leave this module except into the one provider request and the rewritten env file. Every function
takes injectable seams (`http`, paths), so the tests never touch the network or the operator's real files.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from . import scratch as _scratch
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

# provider -> (env var, profile id, models.list URL, header carrying the key, extra headers)
PROVIDERS: dict[str, dict] = {
    "gemini": {
        "env": "GEMINI_API_KEY", "profile": "google:default",
        "url": "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
        "header": "x-goog-api-key", "extra": {},
    },
    "anthropic": {
        "env": "ANTHROPIC_API_KEY", "profile": "anthropic:default",
        "url": "https://api.anthropic.com/v1/models?limit=1",
        "header": "x-api-key", "extra": {"anthropic-version": "2023-06-01"},
    },
}

OK, REJECTED, ERROR, ABSENT = "ok", "rejected", "error", "absent"
HTTP_TIMEOUT_S = 15
RESTART_REASON = "a rewritten gateway.systemd.env key is only read when the gateway starts"

# http(url, headers) returns the HTTP status code, 0 when the call could not be made.
Http = Callable[[str, Mapping[str, str]], int]


def _default_http(url: str, headers: Mapping[str, str]) -> int:
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:     # noqa: S310 - fixed https URLs
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except (urllib.error.URLError, OSError, ValueError):
        return 0


# --- paths -------------------------------------------------------------------------------------------------------

def openclaw_dir() -> Path:
    return Path(os.environ.get("OPENCLAW_STATE_DIR_ROOT") or Path.home() / ".openclaw")


def gateway_env_path() -> Path:
    return openclaw_dir() / "gateway.systemd.env"


def state_db_path() -> Path:
    return openclaw_dir() / "state" / "openclaw.sqlite"


# --- reading the two keys ----------------------------------------------------------------------------------------

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def env_key(name: str, path: Path | None = None) -> str | None:
    """The value of `name` in the gateway env file; None when the file or the entry is absent or empty."""
    try:
        text = Path(path or gateway_env_path()).read_text(encoding="utf-8")
    except OSError:
        return None
    found = None
    for line in text.splitlines():
        m = _LINE.match(line)
        if m and m.group(1) == name:
            found = _unquote(m.group(2))
    return found or None


def profile_key(profile_id: str, db: Path | None = None) -> str | None:
    """The API key of an OpenClaw auth profile, read-only from the state store (`authProfiles.store`).

    Handles a literal `key` and a `keyRef` into the team secret store; any other reference form answers None. Never
    raises: an unreadable store is "no key"."""
    path = Path(db or state_db_path())
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        row = con.execute("select value_json from config_machine_state where state_key = 'authProfiles.store'").fetchone()
        prof = ((json.loads(row[0]).get("profiles") or {}).get(profile_id) or {}) if row else {}
        key = prof.get("key")
        if isinstance(key, str) and key.strip():
            return key.strip()
        ref = prof.get("keyRef")
        if isinstance(ref, dict) and ref.get("source") == "store" and isinstance(ref.get("id"), str):
            r = con.execute("select value from secret_store_entries where scope_kind = 'team' and name = ? "
                            "and deleted_at_ms is null", (ref["id"],)).fetchone()
            if r and isinstance(r[0], str) and r[0].strip():
                return r[0].strip()
    except (sqlite3.Error, ValueError, AttributeError, TypeError):
        return None
    finally:
        con.close()
    return None


# --- probing -----------------------------------------------------------------------------------------------------

def probe(provider: str, key: str | None, http: Http | None = None) -> str:
    """`absent` (no key), `ok`, `rejected` (401/403, or 400 "API key not valid") or `error` (no answer, 5xx, 429)."""
    if not key:
        return ABSENT
    spec = PROVIDERS[provider]
    code = (http or _default_http)(spec["url"], {spec["header"]: key, **spec["extra"]})
    if 200 <= code < 300:
        return OK
    if code in (400, 401, 403):
        return REJECTED
    return ERROR


@dataclass
class KeyResult:
    provider: str
    env: str = ABSENT              # ok | rejected | error | absent
    profile: str = ABSENT
    fixed: bool = False            # the env entry was rewritten from the profile key
    fix_error: str = ""
    backup: str = ""               # path of the backup (a path is not a secret)

    @property
    def mismatch(self) -> bool:
        return self.env == REJECTED and self.profile == OK

    def line(self) -> str:
        """`gemini key: env ok / profile ok`, or `... / mismatch fixed`. Values never appear."""
        parts = [f"env {self.env}", f"profile {self.profile}"]
        if self.fixed:
            parts.append("mismatch fixed")
        elif self.mismatch:
            parts.append("mismatch NOT fixed" + (f" ({self.fix_error})" if self.fix_error else ""))
        return f"{self.provider} key: " + " / ".join(parts)

    @property
    def problem(self) -> bool:
        """Something an operator has to look at: a rejected key left in place, or a profile that does not answer."""
        if self.fixed:
            return False
        return self.env == REJECTED or self.profile == REJECTED


# --- the one repair ----------------------------------------------------------------------------------------------

def rewrite_env_entry(name: str, value: str, path: Path | None = None, *, now: float | None = None) -> str:
    """Replace (or add) `name=value` in the gateway env file. Backup first (0600), atomic replace, mode 0600, every other
    line kept byte for byte. Returns the backup path. Raises OSError/ValueError; the caller reports without the value."""
    if not value or any(c in value for c in "\r\n\0"):
        raise ValueError("refusing to write an empty or multi-line value")
    p = Path(path or gateway_env_path())
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))
    n, backup = 0, p.with_name(f"{p.name}.bak-keycheck-{stamp}")
    while True:                                       # never overwrite an earlier backup (two repairs in one second)
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            n += 1
            backup = p.with_name(f"{p.name}.bak-keycheck-{stamp}-{n}")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(backup, 0o600)
    lines, replaced = [], False
    for line in text.splitlines():
        m = _LINE.match(line)
        if m and m.group(1) == name:
            if not replaced:
                lines.append(f"{name}={value}")
                replaced = True
            continue                                  # a duplicate entry would shadow the repair
        lines.append(line)
    if not replaced:
        lines.append(f"{name}={value}")
    fd, tmp = _scratch.atomic_tmp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return str(backup)


def check(providers: tuple[str, ...] = ("gemini", "anthropic"), *, fix: bool = True, http: Http | None = None,
          env_path: Path | None = None, db: Path | None = None, now: float | None = None) -> list[KeyResult]:
    """Probe both keys of each provider and, with `fix`, reconcile a rejected env key from a working profile key."""
    out: list[KeyResult] = []
    for prov in providers:
        spec = PROVIDERS[prov]
        r = KeyResult(prov)
        ekey = env_key(spec["env"], env_path)
        pkey = profile_key(spec["profile"], db)
        r.env, r.profile = probe(prov, ekey, http), probe(prov, pkey, http)
        if fix and r.mismatch and pkey:
            try:
                r.backup = rewrite_env_entry(spec["env"], pkey, env_path, now=now)
                r.fixed = True
            except (OSError, ValueError) as e:
                r.fix_error = e.__class__.__name__
        out.append(r)
    return out


def env_newer_than_gateway(started_at: float | None, path: Path | None = None) -> str:
    """Why the gateway must restart for its env file ("" when it need not): the file was written after the gateway
    process started, and the process reads `gateway.systemd.env` only at start. Stateless, so a restart that was
    deferred is still owed on the next `ai-resources openclaw reconcile`."""
    if started_at is None:
        return ""
    try:
        mtime = Path(path or gateway_env_path()).stat().st_mtime
    except OSError:
        return ""
    return RESTART_REASON if mtime > started_at else ""


def restart_owed(results: list[KeyResult]) -> bool:
    return any(r.fixed for r in results)


def restart_gateway(*, out: Callable[[str], None] = print) -> str:
    """The drained restart that applies a rewritten env file, through the existing reconcile (restart lock, `watchdog.off`
    on every exit path, busy probe, T29 refusal). Returns "" when it ran or nothing was owed, else the sentence to show
    when it was deferred or did not complete."""
    from . import openclaw_host, openclaw_reconcile, repo_root
    from .setup.cockpits import _shared, openclaw as oc
    plugin_dir = str(Path(_shared.stable_kit_root(repo_root())) / "openclaw-plugin" / "ai-resources")
    outcome = openclaw_reconcile.reconcile(openclaw_host.default_runner, plugin_dir=plugin_dir,
                                           stale_reason=oc.stale_reason, out=out, check_env=True)
    if outcome.deferred or outcome.code:
        return (f"The repaired key applies after a gateway restart: run `{openclaw_reconcile.RECONCILE_COMMAND}` "
                "when the gateway is idle.")
    return ""


# --- CLI ---------------------------------------------------------------------------------------------------------

def cmd_keys(args) -> int:
    results = check(fix=not args.no_fix)
    for r in results:
        print(r.line() + (f"  (env backup: {r.backup})" if r.backup else ""))
    if restart_owed(results):
        msg = restart_gateway()
        if msg:
            print(msg)
    return 1 if any(r.problem for r in results) else 0


def add_subparser(sub) -> None:
    p = sub.add_parser("keys", help="Probe the Gemini and Anthropic keys (gateway env and OpenClaw profile) and repair "
                                    "a rejected env key from the working profile key")
    p.add_argument("--no-fix", action="store_true", help="Report only; do not rewrite gateway.systemd.env")
    p.set_defaults(func=cmd_keys)
