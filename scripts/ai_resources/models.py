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
