"""Background model upgrades for the OpenClaw host.

Discovery (what the catalog offers), policy (what may be applied unattended), patch building, backup,
apply and rollback, smoke probes and the update orchestration. The module has no side effects at
import time, and every external effect (subprocess, restart, clock, lock) is injected so the tests
drive the real state machine against fakes.

Rules that run through the whole module:

* `~/.openclaw/openclaw.json` is NEVER written here. Every change goes through
  `openclaw config patch` (the injected `apply_patch`), which validates the schema. The file copy in
  `backup()` is forensics only.
* Nothing is forced: a busy gateway defers the update, a deferred restart is remembered
  (`state.pending_restart`) and resumed by the next run.
"""
from __future__ import annotations

import copy
import fnmatch
import json
import os
import re
import shutil
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from . import audit, model_pins, openclaw_host
from .model_pins import CLASSES

# (return code, combined output); same contract as openclaw_host.default_runner.
Runner = Callable[..., "tuple[int, str]"]

# --- exit codes: one table, used by the CLI, the wrapper script and the docs ---------------------
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ROLLED_BACK = 2
EXIT_PRECHECK_FAILED = 4
EXIT_ROLLBACK_FAILED = 6
EXIT_APPROVAL_PENDING = 10
EXIT_APPLICABLE = 11          # `models check` only: an auto/approved change is waiting
EXIT_LOCKED = 73
EXIT_DEFERRED = 75

CATALOG_PROVIDER = "claude-cli"
DISCOVERY_TIMEOUT = 90
REFRESH_TIMEOUT = 180
SMOKE_TIMEOUT = 180
HEALTH_TRIES = 36
HEALTH_INTERVAL = 5.0
RESTART_TIMEOUT = 400          # the gateway unit has TimeoutStopSec=330
BACKUPS_KEPT = 10
DEFAULT_COOLDOWN_HOURS = 6

_ANY_FAMILY = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?$")


class DiscoveryError(Exception):
    """The catalog could not be read; the update fails closed and proposes nothing."""


# =================================================================================================
# S5: discovery and policy
# =================================================================================================

@dataclass
class Discovery:
    best: dict[str, str] = field(default_factory=dict)         # class -> highest available id
    new_families: list[str] = field(default_factory=list)      # e.g. claude-mythos-5 (never applied)


def discover(runner: Runner, *, refresh: bool = True, warn: Callable[[str], None] = lambda m: None,
             binary: str = "openclaw") -> Discovery:
    """Highest available Claude id per class from `openclaw models list --all --json --provider claude-cli`."""
    if refresh:
        rc, out = runner([binary, "models", "refresh"], timeout=REFRESH_TIMEOUT)
        if rc != 0:
            warn(f"models refresh failed (rc {rc}); using the cached catalog")
    rc, out = runner([binary, "models", "list", "--all", "--json", "--provider", CATALOG_PROVIDER],
                     timeout=DISCOVERY_TIMEOUT)
    if rc != 0:
        raise DiscoveryError(f"`models list` failed (rc {rc}): {out[-200:]}")
    try:
        data = json.loads(out[out.index("{"):out.rindex("}") + 1])
        entries = data["models"]
        if not isinstance(entries, list):
            raise TypeError("models is not a list")
    except (ValueError, KeyError, TypeError) as e:
        raise DiscoveryError(f"`models list` returned unusable output ({e})") from None
    result = Discovery()
    best_ver: dict[str, tuple[int, int]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("available"):
            continue
        key = str(entry.get("key", ""))
        model_id = key.split("/", 1)[1] if key.startswith(CATALOG_PROVIDER + "/") else key
        parsed = model_pins.parse_id(model_id)
        if parsed:
            cls, major, minor = parsed
            if (major, minor) > best_ver.get(cls, (-1, -1)):
                best_ver[cls], result.best[cls] = (major, minor), model_id
            continue
        m = _ANY_FAMILY.match(model_id)
        if m and m.group(1) not in CLASSES and model_id not in result.new_families:
            result.new_families.append(model_id)
    return result


def _version(model_id: str) -> tuple[int, int] | None:
    p = model_pins.parse_id(model_id)
    return (p[1], p[2]) if p else None


@dataclass
class Proposal:
    cls: str
    old: str
    new: str
    kind: str                       # minor | major | new_family
    price: str                      # equal | lower | higher(N%) | unknown
    decision: str                   # auto | approved | needs_approval | excluded | frozen | report
    reasons: list[str] = field(default_factory=list)

    @property
    def applicable(self) -> bool:
        return self.decision in ("auto", "approved")

    def as_dict(self) -> dict:
        return {"cls": self.cls, "old": self.old, "new": self.new, "kind": self.kind, "price": self.price,
                "decision": self.decision, "reasons": list(self.reasons)}


def _price_delta(old: str, new: str) -> tuple[str, float | None]:
    """Compare list prices (input + output). EXACT ids only: prefix matching would price
    `claude-sonnet-5-5` as `claude-sonnet-5`, and a price must never be invented."""
    p_old, p_new = audit.PRICES.get(old), audit.PRICES.get(new)
    if not p_old or not p_new:
        return "unknown", None
    a, b = p_old[0] + p_old[1], p_new[0] + p_new[1]
    if b == a:
        return "equal", 0.0
    if b < a:
        return "lower", (b - a) / a * 100
    pct = (b - a) / a * 100
    return f"higher({pct:g}%)", pct


def policy_of(overlay: dict) -> dict:
    pol = overlay.get("policy") if isinstance(overlay.get("policy"), dict) else {}
    return {
        "classes": pol.get("classes") if isinstance(pol.get("classes"), dict) else {},
        "max_cost_delta_pct": pol.get("max_cost_delta_pct", 0) or 0,
        "exclude": [g for g in (pol.get("exclude") or []) if isinstance(g, str)],
        "cooldown_hours": pol.get("cooldown_hours", DEFAULT_COOLDOWN_HOURS),
    }


def propose(effective: dict[str, str], discovered: Discovery, overlay: dict | None = None) -> list[Proposal]:
    """One Proposal per class with a newer id in the catalog. A downgrade is never proposed."""
    overlay = overlay or {}
    pol = policy_of(overlay)
    approvals = overlay.get("approvals") if isinstance(overlay.get("approvals"), dict) else {}
    out: list[Proposal] = []
    for cls in CLASSES:
        old, new = effective.get(cls), discovered.best.get(cls)
        if not old or not new:
            continue
        v_old, v_new = _version(old), _version(new)
        if not v_old or not v_new or v_new <= v_old:
            continue
        kind = "major" if v_new[0] > v_old[0] else "minor"
        price, pct = _price_delta(old, new)
        mode = (pol["classes"].get(cls) or {}).get("mode", "auto-minor") if isinstance(pol["classes"].get(cls), dict) else "auto-minor"
        prop = Proposal(cls, old, new, kind, price, "needs_approval")
        if any(fnmatch.fnmatch(new, g) for g in pol["exclude"]):
            prop.decision, prop.reasons = "excluded", ["matches an exclude glob"]
        elif mode == "frozen":
            prop.decision, prop.reasons = "frozen", ["class is frozen"]
        elif approvals.get(cls) == new:
            prop.decision, prop.reasons = "approved", ["approved by the operator"]
        elif ((overlay.get("state") or {}).get("failed") or {}).get(cls) == new:
            prop.reasons = ["a previous attempt failed and was rolled back; approve to retry"]
        else:
            reasons = []
            if kind == "major":
                reasons.append("major jump")
            if mode == "approve":
                reasons.append("mode approve")
            if price == "unknown":
                reasons.append("price unknown")
            elif pct is not None and pct > pol["max_cost_delta_pct"]:
                reasons.append(f"price increase {pct:g}%")
            if reasons:
                prop.reasons = reasons
            else:
                prop.decision, prop.reasons = "auto", [f"{kind} bump, price {price}"]
        out.append(prop)
    for model_id in discovered.new_families:
        out.append(Proposal("new_family", "", model_id, "new_family", "unknown", "report",
                            ["family outside the pinned classes; never applied"]))
    return out


# =================================================================================================
# S6: references, patches, backup, apply and rollback
# =================================================================================================

def collect_refs(doc: dict) -> list[tuple[tuple, str]]:
    """Every model reference in a live config: (path, ref).

    Paths point at the string itself: ("agents","entries","main","model","primary") or
    ("agents","entries","x","model") when the entry uses the bare string form. Fallback lists
    are reported per item as (..., "fallbacks", i)."""
    agents = doc.get("agents") if isinstance(doc.get("agents"), dict) else {}
    out: list[tuple[tuple, str]] = []

    def model_field(base: tuple, owner: dict) -> None:
        value = owner.get("model") if isinstance(owner, dict) else None
        if isinstance(value, str):
            out.append((base + ("model",), value))
        elif isinstance(value, dict):
            if isinstance(value.get("primary"), str):
                out.append((base + ("model", "primary"), value["primary"]))
            fb = value.get("fallbacks")
            if isinstance(fb, list):
                for i, item in enumerate(fb):
                    if isinstance(item, str):
                        out.append((base + ("model", "fallbacks", i), item))

    defaults = agents.get("defaults") if isinstance(agents.get("defaults"), dict) else {}
    model_field(("agents", "defaults"), defaults)
    hb = defaults.get("heartbeat")
    if isinstance(hb, dict):
        model_field(("agents", "defaults", "heartbeat"), hb)
    entries = agents.get("entries") if isinstance(agents.get("entries"), dict) else {}
    for name, entry in entries.items():
        model_field(("agents", "entries", name), entry)
        hb = entry.get("heartbeat") if isinstance(entry, dict) else None
        if isinstance(hb, dict):
            model_field(("agents", "entries", name, "heartbeat"), hb)
    allow = defaults.get("models")
    if isinstance(allow, dict):
        for key in allow:
            out.append((("agents", "defaults", "models", key), key))
    return out


def _get(doc, path):
    cur = doc
    for k in path:
        cur = cur[k]
    return cur


def _nest(patch: dict, path: tuple, value) -> None:
    cur = patch
    for k in path[:-1]:
        cur = cur.setdefault(k, {})
    cur[path[-1]] = value


def build_forward_patch(doc: dict, changes: dict[str, str]) -> dict:
    """{patch, inverse, replace_paths, touched} for `changes = {old_ref: new_ref}`.

    The allowlist gains the new key (copying the old entry) and KEEPS the old one, so a rollback
    stays valid and an agent that pins the old ref explicitly keeps working. Only references equal
    to an old ref are repointed. The inverse restores each repointed value exactly and deletes the
    keys the forward patch added (null)."""
    patch: dict = {}
    inverse: dict = {}
    touched: list[str] = []
    allow = ((doc.get("agents") or {}).get("defaults") or {}).get("models")
    fallback_lists: dict[tuple, list] = {}
    for path, ref in collect_refs(doc):
        if path[:4] == ("agents", "defaults", "models", ref) and len(path) == 4:
            continue
        new = changes.get(ref)
        if not new:
            continue
        touched.append(".".join(str(p) for p in path))
        if "fallbacks" in path:                      # arrays replace as a whole
            list_path = path[:path.index("fallbacks") + 1]
            fallback_lists.setdefault(list_path, list(_get(doc, list_path)))
            fallback_lists[list_path][path[-1]] = new
            continue
        _nest(patch, path, new)
        _nest(inverse, path, ref)
    for list_path, new_list in fallback_lists.items():
        _nest(patch, list_path, new_list)
        _nest(inverse, list_path, copy.deepcopy(_get(doc, list_path)))
    if touched:
        for old, new in changes.items():
            if isinstance(allow, dict) and new not in allow:
                value = copy.deepcopy(allow[old]) if old in allow else {"agentRuntime": {"id": "claude-cli"}}
                _nest(patch, ("agents", "defaults", "models", new), value)
                _nest(inverse, ("agents", "defaults", "models", new), None)
    openclaw_host.assert_channels_safe(patch, [])
    openclaw_host.assert_channels_safe(inverse, [])
    return {"patch": patch, "inverse": inverse, "replace_paths": [], "touched": touched}


def apply_in_memory(doc: dict, patch: dict) -> dict:
    """`openclaw config patch` semantics, for tests and previews: objects merge, null deletes,
    arrays and scalars replace."""
    out = copy.deepcopy(doc)

    def merge(dst: dict, src: dict) -> None:
        for k, v in src.items():
            if v is None:
                dst.pop(k, None)
            elif isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = copy.deepcopy(v)
    merge(out, patch)
    return out


# --- backup ------------------------------------------------------------------------------------

def backup_root() -> Path:
    return Path.home() / ".openclaw" / "backups" / "models-update"


def backup(config_file: Path, *, overlay_file: Path | None = None, root: Path | None = None,
           now: Callable[[], datetime] | None = None, keep: int = BACKUPS_KEPT) -> Path:
    """Copy the live config and the overlay (mode 0600) for forensics; keep the newest `keep`.

    This is a read of openclaw.json and a write elsewhere: nothing here touches the live file."""
    root = root or backup_root()
    stamp = (now() if now else datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
    dest = root / stamp
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    for src in (config_file, overlay_file if overlay_file is not None else model_pins.overlay_path()):
        if src.exists():
            target = dest / src.name
            shutil.copyfile(src, target)
            os.chmod(target, 0o600)
    dirs = sorted(d for d in root.iterdir() if d.is_dir())
    for old in dirs[:-keep] if keep else dirs:
        shutil.rmtree(old, ignore_errors=True)
    return dest


# --- lock --------------------------------------------------------------------------------------

def lock_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime and Path(runtime).is_dir() else model_pins.overlay_path().parent
    return base / "ai-resources-models-update.lock"


@contextmanager
def update_lock(path: Path | None = None) -> Iterator[bool]:
    """Exclusive, non-blocking flock. Yields False when another run holds it."""
    import fcntl
    path = path or lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --- apply and rollback ------------------------------------------------------------------------

ApplyPatch = Callable[..., "tuple[bool, str]"]     # (patch, *, dry_run, replace_paths) -> (ok, output)


def _stamp(now: Callable[[], datetime] | None) -> str:
    return (now() if now else datetime.now(timezone.utc)).isoformat()


def apply_changes(doc: dict, changes: dict[str, tuple[str, str]], *, apply_patch: ApplyPatch,
                  overlay: dict, overlay_file: Path | None = None,
                  now: Callable[[], datetime] | None = None) -> tuple[bool, str, dict]:
    """Switch the config to the new ids, then record it in the overlay.

    `changes` is {cls: (old_id, new_id)}. Order: dry run (any failure aborts, nothing written), real
    patch, and ONLY on success the overlay is saved. Returns (ok, message, overlay)."""
    ref_changes = {model_pins.openclaw_ref(o): model_pins.openclaw_ref(n) for o, n in changes.values()}
    built = build_forward_patch(doc, ref_changes)
    if built["patch"]:
        ok, out = apply_patch(built["patch"], dry_run=True, replace_paths=built["replace_paths"])
        if not ok:
            return False, f"config patch dry run rejected the change: {out[-300:]}", overlay
        ok, out = apply_patch(built["patch"], dry_run=False, replace_paths=built["replace_paths"])
        if not ok:
            return False, f"config patch failed: {out[-300:]}", overlay
    updated = copy.deepcopy(overlay) if overlay else model_pins.empty_overlay()
    previous = {cls: (updated.get("pins") or {}).get(cls) for cls in changes}
    pins = updated.setdefault("pins", {})
    pending = updated.setdefault("pending", {})
    for cls, (_old, new) in changes.items():
        pins[cls] = new
        pending.pop(cls, None)
    st = updated.setdefault("state", {})
    st["last_change"] = {"changes": {c: list(v) for c, v in changes.items()}, "inverse": built["inverse"],
                         "previous_pins": previous, "at": _stamp(now)}
    updated.setdefault("history", []).append(
        {"at": _stamp(now), "action": "switch", "changes": {c: list(v) for c, v in changes.items()}})
    model_pins.save_overlay(updated, overlay_file)
    return True, "applied" if built["patch"] else "recorded (no references to repoint)", updated


def rollback_last(*, apply_patch: ApplyPatch, overlay: dict, overlay_file: Path | None = None,
                  now: Callable[[], datetime] | None = None) -> tuple[bool, str, dict]:
    """Send the recorded inverse patch (dry run first), then restore the previous overlay pins."""
    last = (overlay.get("state") or {}).get("last_change") if overlay else None
    if not isinstance(last, dict) or "inverse" not in last:
        return False, "nothing to roll back (no recorded change)", overlay
    inverse = last["inverse"]
    if inverse:
        ok, out = apply_patch(inverse, dry_run=True, replace_paths=[])
        if not ok:
            return False, f"rollback dry run rejected: {out[-300:]}", overlay
        ok, out = apply_patch(inverse, dry_run=False, replace_paths=[])
        if not ok:
            return False, f"rollback patch failed: {out[-300:]}", overlay
    updated = copy.deepcopy(overlay)
    pins = updated.setdefault("pins", {})
    for cls, prev in (last.get("previous_pins") or {}).items():
        if prev:
            pins[cls] = prev
        else:
            pins.pop(cls, None)
    updated["state"].pop("last_change", None)
    updated.setdefault("history", []).append({"at": _stamp(now), "action": "rollback"})
    model_pins.save_overlay(updated, overlay_file)
    return True, "rolled back", updated


# =================================================================================================
# S7: smoke probe and the update orchestration
# =================================================================================================

class RestartDeferred(Exception):
    """The gateway was not restarted because agent turns are in flight; never forced."""

    def __init__(self, reason: str = ""):
        super().__init__(reason)
        self.reason = reason


@dataclass
class SmokeResult:
    ok: bool
    reason: str = ""
    excerpt: str = ""


_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|(?i:token|key|secret|password|authorization)[\"']?\s*[:=]\s*[\"']?[^\s\"',}]+)")


def redact(text: str, limit: int = 200) -> str:
    return _SECRET.sub("[redacted]", text)[:limit]


def smoke_cwd() -> Path:
    """A neutral scratch directory, so `--setting-sources project` loads no project settings."""
    path = model_pins.overlay_path().parent / "smoke"
    path.mkdir(parents=True, exist_ok=True)
    return path


def smoke(model_id: str, runner: Runner, *, timeout: float = SMOKE_TIMEOUT, claude_bin: str = "claude",
          cwd: Path | None = None) -> SmokeResult:
    """One real call on the operator's account. Passes only when rc is 0, the reply contains OK and
    the served model equals `model_id` (the `modelUsage` key of `claude -p --output-format json`)."""
    argv = [claude_bin, "-p", "--model", model_id, "--output-format", "json", "--setting-sources", "project",
            "Reply with exactly OK"]
    kw = {"timeout": timeout}
    kw["cwd"] = str(cwd or smoke_cwd())
    rc, out = runner(argv, **kw)
    if rc != 0:
        return SmokeResult(False, f"claude exited {rc}", redact(out))
    try:
        data = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except ValueError:
        return SmokeResult(False, "reply was not JSON", redact(out))
    if not isinstance(data, dict) or data.get("is_error"):
        return SmokeResult(False, "claude reported an error", redact(str(data.get("result", "")) if isinstance(data, dict) else out))
    if "OK" not in str(data.get("result", "")):
        return SmokeResult(False, "reply did not contain OK", redact(str(data.get("result", ""))))
    served = list((data.get("modelUsage") or {}).keys())
    if not any(k.split("[", 1)[0] == model_id for k in served):
        return SmokeResult(False, f"served model {served or 'unknown'} is not {model_id}", redact(str(data.get("result", ""))))
    return SmokeResult(True, "ok", redact(str(data.get("result", ""))))


@dataclass
class Options:
    check: bool = False
    dry_run: bool = False
    classes: list[str] | None = None
    no_restart: bool = False
    refresh: bool = True
    unattended: bool = False


@dataclass
class Result:
    rc: int
    outcome: str
    message: str = ""
    proposals: list[Proposal] = field(default_factory=list)
    applied: dict[str, tuple[str, str]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"rc": self.rc, "outcome": self.outcome, "message": self.message,
                "proposals": [p.as_dict() for p in self.proposals],
                "applied": {c: list(v) for c, v in self.applied.items()}}


@dataclass
class Deps:
    """Everything external. `default_deps()` wires the real ones; tests pass fakes."""
    runner: Runner
    apply_patch: ApplyPatch
    read_config: Callable[[], dict]
    config_file: Callable[[], Path]
    smoke: Callable[[str], SmokeResult]
    busy: Callable[[], int]
    restart: Callable[[], object]
    health: Callable[[], bool]
    main_pid: Callable[[], str]
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    marker: openclaw_host.Marker | None = None
    overlay_file: Path | None = None
    backup_root: Path | None = None
    lock: Callable[[], object] = update_lock
    log: Callable[[dict], None] = lambda event: None
    health_tries: int = HEALTH_TRIES
    health_interval: float = HEALTH_INTERVAL


def _load(deps: Deps) -> dict:
    ov = model_pins.load_overlay(deps.overlay_file)
    return ov or model_pins.empty_overlay()


def _persist(deps: Deps, ov: dict, **state_fields) -> dict:
    ov.setdefault("state", {}).update(state_fields)
    model_pins.save_overlay(ov, deps.overlay_file)
    return ov


def _log(deps: Deps, event: str, **kw) -> None:
    deps.log({"at": deps.now().isoformat(), "event": event, **kw})


def run_update(opts: Options, deps: Deps) -> Result:
    with deps.lock() as got:
        if not got:
            return Result(EXIT_LOCKED, "locked", "another models update is running")
        return _run_locked(opts, deps)


def _run_locked(opts: Options, deps: Deps) -> Result:
    writes = not (opts.check or opts.dry_run)
    ov = _load(deps)
    st = ov.get("state") or {}
    now = deps.now()

    if st.get("pending_restart") and writes:
        last = st.get("last_change") or {}
        applied = {c: tuple(v) for c, v in (last.get("changes") or {}).items()}
        _log(deps, "resume_restart", applied=list(applied))
        return _restart_and_verify(opts, deps, ov, applied, resume=True)

    cooldown = policy_of(ov)["cooldown_hours"]
    last_switch = st.get("last_switch_at")
    if writes and last_switch:
        try:
            if now - datetime.fromisoformat(last_switch) < timedelta(hours=cooldown):
                _log(deps, "cooldown", last_switch_at=last_switch)
                return Result(EXIT_OK, "cooldown", f"switched less than {cooldown:g} h ago")
        except ValueError:
            pass

    try:
        disc = discover(deps.runner, refresh=opts.refresh, warn=lambda m: _log(deps, "warn", message=m))
    except DiscoveryError as e:
        _log(deps, "discovery_failed", error=str(e))
        if writes:
            _persist(deps, ov, last_run=now.isoformat(), last_result="error")
        return Result(EXIT_ERROR, "error", str(e))
    props = propose(model_pins.effective(ov), disc, ov)
    needs = [p for p in props if p.decision == "needs_approval"]
    if writes:
        pending = ov.setdefault("pending", {})
        for cls in list(pending):
            if cls not in {p.cls for p in needs}:
                pending.pop(cls)
        for p in needs:
            first = (pending.get(p.cls) or {}).get("first_seen") if (pending.get(p.cls) or {}).get("to") == p.new else None
            pending[p.cls] = {"to": p.new, "reason": ", ".join(p.reasons), "first_seen": first or now.isoformat()}
    applicable = [p for p in props if p.applicable and (not opts.classes or p.cls in opts.classes)]
    _log(deps, "plan", proposals=[p.as_dict() for p in props])

    if opts.classes:
        needs = [p for p in needs if p.cls in opts.classes]       # only what the caller asked about
    if not applicable:
        rc = EXIT_APPROVAL_PENDING if needs else EXIT_OK
        if writes:
            _persist(deps, ov, last_run=now.isoformat(), last_result="pending_approval" if needs else "no_change")
        return Result(rc, "pending_approval" if needs else "no_change", proposals=props)
    if opts.check:
        return Result(EXIT_APPLICABLE, "applicable", "changes are ready to apply", proposals=props)

    survivors: list[Proposal] = []
    for p in applicable:
        res = deps.smoke(p.new)
        _log(deps, "smoke", model=p.new, ok=res.ok, reason=res.reason, excerpt=res.excerpt)
        if res.ok:
            survivors.append(p)
    if not survivors:
        if writes:
            _persist(deps, ov, last_run=now.isoformat(), last_result="smoke_failed")
        return Result(EXIT_PRECHECK_FAILED, "smoke_failed", "smoke failed for every candidate", proposals=props)
    changes = {p.cls: (p.old, p.new) for p in survivors}

    if deps.busy():
        if opts.dry_run:
            return Result(EXIT_DEFERRED, "deferred", "the gateway is busy", proposals=props)
        _persist(deps, ov, last_run=now.isoformat(), last_result="deferred",
                 deferrals=int(st.get("deferrals", 0)) + 1)
        _log(deps, "deferred", reason="gateway busy")
        return Result(EXIT_DEFERRED, "deferred", "the gateway is busy; nothing was patched", proposals=props)

    doc = deps.read_config()
    if opts.dry_run:
        built = build_forward_patch(doc, {model_pins.openclaw_ref(o): model_pins.openclaw_ref(n)
                                          for o, n in changes.values()})
        if built["patch"]:
            ok, out = deps.apply_patch(built["patch"], dry_run=True, replace_paths=[])
            if not ok:
                return Result(EXIT_PRECHECK_FAILED, "patch_rejected", out[-300:], proposals=props)
        return Result(EXIT_OK, "dry_run", "the patch validates; nothing was applied", proposals=props, applied=changes)

    backup(deps.config_file(), overlay_file=deps.overlay_file, root=deps.backup_root, now=deps.now)
    ok, msg, ov = apply_changes(doc, changes, apply_patch=deps.apply_patch, overlay=ov,
                                overlay_file=deps.overlay_file, now=deps.now)
    if not ok:
        _persist(deps, ov, last_run=now.isoformat(), last_result="patch_rejected")
        _log(deps, "apply_failed", message=msg)
        return Result(EXIT_PRECHECK_FAILED, "patch_rejected", msg, proposals=props)
    _log(deps, "applied", changes={c: list(v) for c, v in changes.items()})
    if opts.no_restart:
        _persist(deps, ov, last_run=now.isoformat(), last_result="switched", last_switch_at=now.isoformat())
        return Result(EXIT_OK, "switched", "switched; the gateway was not restarted (--no-restart)",
                      proposals=props, applied=changes)
    _persist(deps, ov, pending_restart=True, pending_restart_pid=deps.main_pid())
    return _restart_and_verify(opts, deps, ov, changes, resume=False, proposals=props)


def _await_health(deps: Deps) -> bool:
    for _ in range(deps.health_tries):
        if deps.health():
            return True
        deps.sleep(deps.health_interval)
    return False


def _restart_and_verify(opts: Options, deps: Deps, ov: dict, applied: dict[str, tuple[str, str]], *,
                        resume: bool, proposals: list[Proposal] | None = None) -> Result:
    proposals = proposals or []
    now = deps.now()
    st = ov.get("state") or {}
    marker = deps.marker or openclaw_host.Marker()
    owned = not marker.exists()
    marker.touch()
    try:
        pid_before = st.get("pending_restart_pid") or deps.main_pid()
        already = resume and pid_before and deps.main_pid() != pid_before
        if not already:
            try:
                deps.restart()
            except RestartDeferred as e:
                _persist(deps, ov, pending_restart=True, pending_restart_pid=pid_before, last_run=now.isoformat(),
                         last_result="deferred", deferrals=int(st.get("deferrals", 0)) + 1)
                _log(deps, "restart_deferred", reason=e.reason)
                return Result(EXIT_DEFERRED, "deferred", f"restart deferred: {e.reason}", proposals, applied)
        healthy = _await_health(deps)
        pid_changed = already or not pid_before or deps.main_pid() != pid_before
        reason = "" if healthy and pid_changed else ("gateway unhealthy after restart" if not healthy
                                                     else "gateway pid did not change")
        if not reason:
            for cls, (_old, new) in applied.items():
                res = deps.smoke(new)
                _log(deps, "smoke_post", model=new, ok=res.ok, reason=res.reason)
                if not res.ok:
                    reason = f"post-switch smoke failed for {new}: {res.reason}"
                    break
        if not reason:
            ov.get("state", {}).pop("pending_restart", None)
            ov["state"].pop("pending_restart_pid", None)
            _persist(deps, ov, deferrals=0, last_switch_at=now.isoformat(), last_run=now.isoformat(),
                     last_result="switched")
            ov.setdefault("history", []).append({"at": now.isoformat(), "action": "verified"})
            model_pins.save_overlay(ov, deps.overlay_file)
            _log(deps, "switched", changes={c: list(v) for c, v in applied.items()})
            return Result(EXIT_OK, "switched", "switched and healthy", proposals, applied)

        _log(deps, "switch_failed", reason=reason)
        ok, msg, back = rollback_last(apply_patch=deps.apply_patch, overlay=ov, overlay_file=deps.overlay_file,
                                      now=deps.now)
        if not ok:
            _persist(deps, ov, last_run=now.isoformat(), last_result="rollback_failed")
            _log(deps, "rollback_failed", message=msg)
            return Result(EXIT_ROLLBACK_FAILED, "rollback_failed", f"{reason}; {msg}", proposals, applied)
        back["state"].pop("pending_restart", None)
        back["state"].pop("pending_restart_pid", None)
        failed = back["state"].setdefault("failed", {})
        for cls, (_old, new) in applied.items():
            failed[cls] = new
        try:
            deps.restart()
            healthy_again = _await_health(deps)
        except RestartDeferred:
            healthy_again = False
        _persist(deps, back, last_run=now.isoformat(), deferrals=0,
                 last_result="rolled_back" if healthy_again else "rollback_failed")
        _log(deps, "rolled_back" if healthy_again else "rollback_failed", reason=reason)
        if not healthy_again:
            return Result(EXIT_ROLLBACK_FAILED, "rollback_failed", f"{reason}; the gateway is not healthy after the rollback",
                          proposals, applied)
        return Result(EXIT_ROLLED_BACK, "rolled_back", f"{reason}; rolled back", proposals, applied)
    finally:
        if owned:
            marker.remove()


# --- real wiring -------------------------------------------------------------------------------

def log_file() -> Path:
    base = os.environ.get("OPENCLAW_LOG_DIR")
    return (Path(base) if base else Path.home() / ".openclaw" / "logs") / "models-update.log"


def _append_log(event: dict) -> None:
    try:
        path = log_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
    except OSError:
        pass


def default_deps() -> Deps:
    """The real wiring. Imports the OpenClaw cockpit lazily: it is heavy and the pure functions
    above must stay importable without it."""
    from .setup.cockpits import openclaw as oc
    runner = openclaw_host.default_runner

    def restart() -> object:
        try:
            return oc.restart_gateway(backend="claude-cli", ask=None, interactive=False,
                                      restart_timeout=RESTART_TIMEOUT)
        except oc.RestartDeferred as e:
            raise RestartDeferred(e.reason) from None

    def health() -> bool:
        rc, _ = runner([openclaw_host.resolve_openclaw_bin(), "health"], timeout=60)
        return rc == 0

    return Deps(
        runner=runner, apply_patch=oc.apply_patch, read_config=lambda: oc.read_config(oc.config_path()),
        config_file=oc.config_path, smoke=lambda m: smoke(m, runner, claude_bin=shutil.which("claude") or "claude"),
        busy=lambda: openclaw_host.gateway_busy()[0], restart=restart, health=health,
        main_pid=openclaw_host.gateway_main_pid, log=_append_log)


def run_rollback(deps: Deps) -> Result:
    """`models rollback`: the recorded inverse patch, then the same restart and health flow."""
    with deps.lock() as got:
        if not got:
            return Result(EXIT_LOCKED, "locked", "another models update is running")
        ov = _load(deps)
        ok, msg, back = rollback_last(apply_patch=deps.apply_patch, overlay=ov, overlay_file=deps.overlay_file,
                                      now=deps.now)
        if not ok:
            return Result(EXIT_PRECHECK_FAILED, "nothing_to_roll_back", msg)
        _log(deps, "manual_rollback")
        marker = deps.marker or openclaw_host.Marker()
        owned = not marker.exists()
        marker.touch()
        try:
            try:
                deps.restart()
            except RestartDeferred as e:
                _persist(deps, back, pending_restart=True, last_result="rolled_back")
                return Result(EXIT_DEFERRED, "deferred", f"rolled back; restart deferred: {e.reason}")
            healthy = _await_health(deps)
        finally:
            if owned:
                marker.remove()
        _persist(deps, back, last_result="rolled_back" if healthy else "rollback_failed",
                 last_run=deps.now().isoformat())
        if not healthy:
            return Result(EXIT_ROLLBACK_FAILED, "rollback_failed", "rolled back, but the gateway is not healthy")
        return Result(EXIT_OK, "rolled_back", "rolled back and healthy")
