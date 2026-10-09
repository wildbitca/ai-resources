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

from . import audit, model_fanout, model_pins, model_providers, models_interaction, openclaw_host
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
DISCOVERY_BUDGET = 90          # one budget for the whole discovery run, not per provider
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
    best: dict[str, str] = field(default_factory=dict)         # Claude class -> highest available id
    new_families: list[str] = field(default_factory=list)      # e.g. claude-mythos-5 (never applied)
    others: dict[str, str] = field(default_factory=dict)       # non-Claude slot -> highest stable id
    previews: dict[str, str] = field(default_factory=dict)     # slot -> newest preview/alias (report only)
    providers: list = field(default_factory=list)              # one ProviderRow per provider asked about
    listed: dict = field(default_factory=dict)                 # provider -> every available id seen (for doctor)

    def slot_best(self) -> dict[str, str]:
        """slot -> highest stable id, Claude slots first."""
        out = {model_pins.slot_key(c): i for c, i in self.best.items()}
        out.update(self.others)
        return out


@dataclass
class ProviderRow:
    """What discovery did for one provider; shown by status/check/update and in the JSON report."""
    provider: str
    status: str                # ok | skipped | report-only | failed
    detail: str = ""
    count: int = 0

    @property
    def text(self) -> str:
        if self.status == "ok":
            return f"{self.count} model(s) listed"
        return f"skipped: {self.detail}" if self.status == "skipped" else self.detail

    def as_dict(self) -> dict:
        return {"provider": self.provider, "status": self.status, "detail": self.detail, "count": self.count}


def _read_catalog(runner: Runner, binary: str, catalog_id: str, timeout: float) -> list:
    rc, out = runner([binary, "models", "list", "--all", "--json", "--provider", catalog_id], timeout=timeout)
    if rc != 0:
        raise DiscoveryError(f"`models list` failed (rc {rc}): {out[-200:]}")
    try:
        data = json.loads(out[out.index("{"):out.rindex("}") + 1])
        entries = data["models"]
        if not isinstance(entries, list):
            raise TypeError("models is not a list")
    except (ValueError, KeyError, TypeError) as e:
        raise DiscoveryError(f"`models list` returned unusable output ({e})") from None
    return entries


def _absorb_claude(result: "Discovery", entries: list, best_ver: dict) -> None:
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("available"):
            continue
        key = str(entry.get("key", ""))
        model_id = key.split("/", 1)[1] if key.startswith(CATALOG_PROVIDER + "/") else key
        result.listed.setdefault("anthropic", set()).add(model_id)
        parsed = model_pins.parse_id(model_id)
        if parsed:
            cls, major, minor = parsed
            if (major, minor) > best_ver.get(cls, (-1, -1)):
                best_ver[cls], result.best[cls] = (major, minor), model_id
            continue
        m = _ANY_FAMILY.match(model_id)
        if m and m.group(1) not in CLASSES and model_id not in result.new_families:
            result.new_families.append(model_id)


def _absorb_provider(result: "Discovery", adapter, ids: list[str], wanted: set[str]) -> int:
    """Fold a provider's model ids into the discovery. Only slots in `wanted` get a best id."""
    seen = 0
    best_ver: dict[str, tuple] = {}
    prev_ver: dict[str, tuple] = {}
    for model_id in ids:
        ref = adapter.from_any_spelling(model_id)
        seen += 1
        result.listed.setdefault(adapter.id, set()).add(model_id)
        if ref is None:
            tag = f"{adapter.id}/{model_id}"
            if tag not in result.new_families and ":" not in model_id:
                result.new_families.append(tag)
            continue
        if ref.slot not in wanted:
            continue
        if ref.channel == "stable":
            if ref.version > best_ver.get(ref.slot, ()):
                best_ver[ref.slot], result.others[ref.slot] = ref.version, model_id
        elif ref.channel == "preview" and ref.version > prev_ver.get(ref.slot, ()):
            prev_ver[ref.slot], result.previews[ref.slot] = ref.version, model_id
    return seen


def _vendor_ids(adapter, key: str, http, timeout: float) -> list[str]:
    """Model ids from the vendor's own `/models`. The host is the adapter's constant; the key travels
    only in the Authorization header and is never logged or returned."""
    status, text = http("GET", adapter.vendor_host + adapter.vendor_path,
                        {"Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout)
    if status != 200:
        raise DiscoveryError(f"{adapter.id} /models answered HTTP {status}")
    try:
        data = json.loads(text)
        return [str(m["id"]) for m in data["data"] if isinstance(m, dict) and "id" in m]
    except (ValueError, KeyError, TypeError):
        raise DiscoveryError(f"{adapter.id} /models returned unusable output") from None


def make_http(transport=None) -> Callable:
    """The HTTP seam: `http(method, url, headers, timeout) -> (status, text)`. httpx is imported
    lazily (the pure functions above stay importable without it); tests pass a MockTransport."""
    def http(method: str, url: str, headers: dict, timeout: float, body: dict | None = None) -> tuple[int, str]:
        import httpx
        with httpx.Client(transport=transport, timeout=timeout) as client:
            r = client.request(method, url, headers=headers, json=body)
            return r.status_code, r.text
    return http


def discover(runner: Runner, *, refresh: bool = True, warn: Callable[[str], None] = lambda m: None,
             binary: str = "openclaw", selection=None, accounts: dict | None = None, http: Callable | None = None,
             key_for: Callable[[str], str] = lambda p: "", monotonic: Callable[[], float] = time.monotonic,
             budget: float = DISCOVERY_BUDGET) -> "Discovery":
    """Highest stable id per slot.

    Without a `selection` this is the original Claude-only discovery (`models list --provider
    claude-cli`; any failure raises DiscoveryError). With one, only providers that are ENABLED and
    CREDENTIALED are asked: one refresh, then each provider's sources in order (OpenClaw catalog,
    the vendor listing when the user opted in, else a static report-only list). A provider that
    fails is recorded as `discovery failed`; the run raises only when every provider it asked failed.
    One time budget covers the whole run, not each provider.
    """
    if selection is None:
        if refresh:
            rc, out = runner([binary, "models", "refresh"], timeout=REFRESH_TIMEOUT)
            if rc != 0:
                warn(f"models refresh failed (rc {rc}); using the cached catalog")
        result = Discovery()
        _absorb_claude(result, _read_catalog(runner, binary, CATALOG_PROVIDER, DISCOVERY_TIMEOUT), {})
        result.providers.append(ProviderRow("anthropic", "ok", "", len(result.best)))
        # A host with no recorded selection runs the Claude classes only: say so for the rest.
        for pid in model_providers.model_families():
            if pid != "anthropic":
                result.providers.append(ProviderRow(pid, "skipped", "not enabled"))
        return result

    accounts = accounts or {}
    result = Discovery()
    deadline = monotonic() + budget
    wanted = set(selection.slots)
    enabled = set(selection.enabled_providers())
    asked = failed = 0
    refreshed = False
    for pid, adapter in model_providers.REGISTRY.items():
        if pid not in enabled:
            if pid != "openrouter":
                result.providers.append(ProviderRow(pid, "skipped", "not enabled"))
            continue
        acc = accounts.get(pid)
        if acc is not None and not getattr(acc, "credentialed", False):
            result.providers.append(ProviderRow(pid, "skipped", "no credentials"))
            continue
        row = None
        for source in adapter.sources:
            if source == model_providers.SOURCE_CATALOG and adapter.catalog_ids:
                if monotonic() >= deadline:
                    row = ProviderRow(pid, "failed", "discovery failed: budget")
                    break
                if refresh and not refreshed:
                    refreshed = True
                    rc, _out = runner([binary, "models", "refresh"], timeout=min(REFRESH_TIMEOUT, max(1.0, deadline - monotonic())))
                    if rc != 0:
                        warn(f"models refresh failed (rc {rc}); using the cached catalog")
                asked += 1
                catalog_id = CATALOG_PROVIDER if pid == "anthropic" else adapter.catalog_ids[0]
                try:
                    entries = _read_catalog(runner, binary, catalog_id, min(DISCOVERY_TIMEOUT, max(1.0, deadline - monotonic())))
                except DiscoveryError as e:
                    failed += 1
                    row = ProviderRow(pid, "failed", f"discovery failed: {e}")
                    warn(f"{pid}: {e}")
                    break
                if pid == "anthropic":
                    before = len(result.best)
                    _absorb_claude(result, entries, {})
                    count = len(result.best) or before
                else:
                    ids = [str(e.get("key", "")).split("/", 1)[-1] for e in entries
                           if isinstance(e, dict) and e.get("available")]
                    count = _absorb_provider(result, adapter, ids, wanted)
                row = ProviderRow(pid, "ok", "", count)
                if count == 0:
                    continue            # an empty catalog entry (openai here) falls through to the next source
                break
            if source == model_providers.SOURCE_VENDOR and adapter.vendor_host:
                psel = selection.providers.get(pid)
                if not (psel and psel.vendor_listing):
                    continue
                key = key_for(pid)
                if not key or http is None:
                    continue
                if monotonic() >= deadline:
                    row = ProviderRow(pid, "failed", "discovery failed: budget")
                    break
                asked += 1
                try:
                    ids = _vendor_ids(adapter, key, http, min(DISCOVERY_TIMEOUT, max(1.0, deadline - monotonic())))
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    row = ProviderRow(pid, "failed", f"discovery failed: {type(e).__name__}" if not isinstance(e, DiscoveryError)
                                      else f"discovery failed: {e}")
                    break
                row = ProviderRow(pid, "ok", "", _absorb_provider(result, adapter, ids, wanted))
                break
        if row is None or (row.status == "ok" and row.count == 0 and model_providers.SOURCE_STATIC in adapter.sources):
            row = ProviderRow(pid, "report-only", "static list, report-only")
        result.providers.append(row)
    if asked and failed == asked:
        raise DiscoveryError("every enabled provider failed: " + "; ".join(
            f"{r.provider} ({r.detail})" for r in result.providers if r.status == "failed"))
    return result


def _version(model_id: str) -> tuple[int, int] | None:
    p = model_pins.parse_id(model_id)
    return (p[1], p[2]) if p else None


@dataclass
class Proposal:
    cls: str                        # the family: opus | sonnet | gemini-flash | ...
    old: str
    new: str
    kind: str                       # minor | major | new_family | current
    price: str                      # equal | lower | higher(N%) | unknown
    decision: str                   # auto | approved | needs_approval | excluded | frozen | suppressed | report | current
    reasons: list[str] = field(default_factory=list)
    provider: str = "anthropic"

    @property
    def slot(self) -> str:
        return f"{self.provider}:{self.cls}"

    @property
    def applicable(self) -> bool:
        return self.decision in ("auto", "approved")

    def as_dict(self) -> dict:
        # `cls` is kept for the wrapper script and older readers; `slot` and `provider` are additive.
        return {"cls": self.cls, "slot": self.slot, "provider": self.provider, "old": self.old, "new": self.new,
                "kind": self.kind, "price": self.price, "decision": self.decision, "reasons": list(self.reasons)}


def _price_of(model_id: str, overlay: dict | None) -> tuple | None:
    """List price (in, out) per 1M tokens: the audit table, then the operator's own `prices`.
    EXACT ids only: prefix matching would price `claude-sonnet-5-5` as `claude-sonnet-5`."""
    price = audit.PRICES.get(model_id)
    if price:
        return price
    own = (overlay or {}).get("prices") if isinstance((overlay or {}).get("prices"), dict) else {}
    entry = own.get(model_id)
    if isinstance(entry, (list, tuple)) and len(entry) >= 2 and all(isinstance(x, (int, float)) for x in entry[:2]):
        return tuple(entry)
    return None


def _price_delta(old: str, new: str, overlay: dict | None = None) -> tuple[str, float | None]:
    """Compare list prices (input + output). A price must never be invented."""
    p_old, p_new = _price_of(old, overlay), _price_of(new, overlay)
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
    overlay = model_pins.migrate(overlay) if overlay else {}
    pol = overlay.get("policy") if isinstance(overlay.get("policy"), dict) else {}
    return {
        "slots": pol.get("slots") if isinstance(pol.get("slots"), dict) else {},
        "max_cost_delta_pct": pol.get("max_cost_delta_pct", 0) or 0,
        "exclude": [g for g in (pol.get("exclude") or []) if isinstance(g, str)],
        "cooldown_hours": pol.get("cooldown_hours", DEFAULT_COOLDOWN_HOURS),
    }


def _adapter_version(provider: str, model_id: str) -> tuple[int, ...] | None:
    adapter = model_providers.REGISTRY.get(provider)
    ref = adapter.from_any_spelling(model_id) if adapter else None
    return ref.version if ref else None


def _slot_track(selection, slot: str) -> str:
    slots = getattr(selection, "slots", None) or {}
    sel = slots.get(slot)
    if sel is None:
        return "family"
    return (sel.get("track") if isinstance(sel, dict) else getattr(sel, "track", None)) or "family"


def propose(effective: dict[str, str], discovered: Discovery, overlay: dict | None = None, selection=None,
            accounts: dict | None = None) -> list[Proposal]:
    """One Proposal per slot. A downgrade is never proposed.

    `effective` maps slots (or bare Claude class names) to the id each slot runs now. The ladder, in
    order: exclude glob, frozen, never (this id, then the whole family), approved, failed retry, then
    the policy `answer`: `ask` waits for a human, `always` applies within `max_bump` when the price is
    known and not higher. A slot with nothing newer reports `current`.
    """
    overlay = model_pins.migrate(overlay) if overlay else {}
    pol = policy_of(overlay)
    approvals = overlay.get("approvals") if isinstance(overlay.get("approvals"), dict) else {}
    failed = ((overlay.get("state") or {}).get("failed")) or {}
    best_by_slot = discovered.slot_best()
    out: list[Proposal] = []
    for key, old in effective.items():
        slot = model_pins.slot_key(key)
        provider, cls = slot.split(":", 1)
        new = best_by_slot.get(slot)
        if not old or not new:
            continue
        v_old, v_new = _adapter_version(provider, old), _adapter_version(provider, new)
        if not v_old or not v_new:
            continue
        if _slot_track(selection, slot) == "fixed":
            # A fixed-track slot is never rewritten: report it as current, propose nothing.
            out.append(Proposal(cls, old, old, "current", "equal", "current", ["track is fixed"], provider))
            continue
        if v_new <= v_old:
            # Nothing newer in the catalog: say so instead of leaving the slot out of the report.
            reasons = [] if v_new == v_old else [f"catalog newest is {new}"]
            out.append(Proposal(cls, old, old, "current", "equal", "current", reasons, provider))
            continue
        kind = "major" if v_new[0] > v_old[0] else "minor"
        price, pct = _price_delta(old, new, overlay)
        sp = model_pins.slot_policy(overlay, slot)
        prop = Proposal(cls, old, new, kind, price, "needs_approval", [], provider)
        if any(fnmatch.fnmatch(new, g) for g in pol["exclude"]):
            prop.decision, prop.reasons = "excluded", ["matches an exclude glob"]
        elif sp.get("frozen"):
            prop.decision, prop.reasons = "frozen", ["class is frozen"]
        elif new in sp.get("never_ids", []) or sp.get("family_never") or sp.get("answer") == "never":
            prop.decision, prop.reasons = "suppressed", ["you chose never for this " +
                                                         ("family" if sp.get("family_never") or sp.get("answer") == "never"
                                                          else "model")]
        elif approvals.get(slot) == new:
            prop.decision, prop.reasons = "approved", ["approved by the operator"]
        elif failed.get(slot) == new:
            prop.reasons = ["a previous attempt failed and was rolled back; approve to retry"]
        else:
            reasons = []
            if kind == "major" and sp.get("max_bump", "minor") == "minor":
                reasons.append("major jump")
            if sp.get("answer") != "always":
                reasons.append("mode approve")
            if price == "unknown":
                reasons.append("price unknown")
            elif pct is not None and pct > pol["max_cost_delta_pct"]:
                reasons.append(f"price increase {pct:g}%")
            if selection is not None and smoke_path_problem(provider, selection, accounts):
                reasons.append("no smoke path")
            if reasons:
                prop.reasons = reasons
            else:
                prop.decision, prop.reasons = "auto", [f"{kind} bump, price {price}"]
        out.append(prop)
    # A preview or alias newer than what the slot runs is reported, never applied or proposed.
    for key, old in effective.items():
        slot = model_pins.slot_key(key)
        preview = discovered.previews.get(slot)
        if not old or not preview:
            continue
        provider, cls = slot.split(":", 1)
        v_old, v_prev = _adapter_version(provider, old), _adapter_version(provider, preview)
        if v_old and v_prev and v_prev > v_old:
            out.append(Proposal(cls, old, preview, "preview", "unknown", "report",
                                ["newest is a preview; never applied automatically"], provider))
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


def _runtime_of(ref: str) -> str:
    """agentRuntime id for a new allowlist key: the adapter's, `claude-cli` for anything unknown."""
    prefix = ref.split("/", 1)[0]
    for adapter in model_providers.REGISTRY.values():
        if adapter.ref_prefix == prefix and adapter.runtime:
            return adapter.runtime
    return "claude-cli"


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
                value = copy.deepcopy(allow[old]) if old in allow else {"agentRuntime": {"id": _runtime_of(new)}}
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

def slot_ref(slot: str, model_id: str) -> str | None:
    """The OpenClaw model ref for `model_id` in `slot`, or None when OpenClaw has no verified runtime
    for that provider (such a slot is never repointed in openclaw.json)."""
    provider = model_pins.slot_provider(slot)
    adapter = model_providers.REGISTRY.get(provider)
    if provider == "anthropic":
        return model_pins.openclaw_ref(model_id)
    if not adapter or not adapter.ref_prefix or not adapter.runtime:
        return None
    return f"{adapter.ref_prefix}/{model_id}"


ApplyPatch = Callable[..., "tuple[bool, str]"]     # (patch, *, dry_run, replace_paths) -> (ok, output)


def _stamp(now: Callable[[], datetime] | None) -> str:
    return (now() if now else datetime.now(timezone.utc)).isoformat()


@dataclass
class ApplyOutcome:
    ok: bool
    message: str
    overlay: dict
    failed: str = ""                  # artifact id that failed ("" when ok)
    unwound: bool = False             # an artifact AFTER the first failed and the earlier ones were restored
    rendered: list = field(default_factory=list)    # ids of the artifacts written besides the overlay
    restore_failed: bool = False      # a restore after the failure itself failed: files are left half-applied


def registry(apply_patch: ApplyPatch, extra: list | None = None) -> list:
    """The ordered artifact registry: openclaw first, then whatever S12 registers, overlay last."""
    return [model_fanout.OpenClawArtifact(apply_patch, build_forward_patch, slot_ref), *(extra or [])]


def apply_changes_ex(doc: dict, changes: dict[str, tuple[str, str]], *, apply_patch: ApplyPatch,
                     overlay: dict, overlay_file: Path | None = None,
                     now: Callable[[], datetime] | None = None, artifacts: list | None = None,
                     selection=None, plan: dict | None = None, backup_dir: Path | None = None) -> ApplyOutcome:
    """Switch every affected artifact to the new ids, then record it in the overlay.

    `changes` is {slot: (old_id, new_id)} (a bare Claude class name is accepted for an anthropic
    slot). Order: the registry renders in order (openclaw: dry run, then the real patch), a failure
    restores the earlier artifacts in reverse, and ONLY on success the overlay is saved. A slot whose
    provider has no verified OpenClaw runtime is recorded in the overlay but never repointed in
    openclaw.json."""
    changes = {model_pins.slot_key(k): v for k, v in changes.items()}
    arts = registry(apply_patch, artifacts)
    ctx = model_fanout.Ctx(doc=doc, selection=selection, plan=plan or {},
                           extras={"backup_dir": backup_dir})
    res = model_fanout.apply_all(arts, model_fanout.Change(changes), ctx)
    if not res.ok:
        restore_failed = bool(res.restore_errors)
        unwound = bool(res.restored) and res.failed != arts[0].id and not restore_failed
        return ApplyOutcome(False, res.message + ("; " + "; ".join(res.restore_errors) if res.restore_errors else ""),
                            overlay, res.failed, unwound, restore_failed=restore_failed)
    openclaw_payload = res.payloads.get("openclaw", {"inverse": {}})
    updated = model_pins.migrate(overlay) if overlay else model_pins.empty_overlay()
    previous = {slot: (updated.get("pins") or {}).get(slot) for slot in changes}
    pins = updated.setdefault("pins", {})
    pending = updated.setdefault("pending", {})
    for slot, (_old, new) in changes.items():
        pins[slot] = new
        pending.pop(slot, None)
    st = updated.setdefault("state", {})
    st["last_change"] = {"changes": {c: list(v) for c, v in changes.items()},
                         "inverse": openclaw_payload.get("inverse", {}),
                         "artifacts": res.payloads,
                         "previous_pins": previous, "at": _stamp(now)}
    updated.setdefault("history", []).append(
        {"at": _stamp(now), "action": "switch", "changes": {c: list(v) for c, v in changes.items()}})
    model_pins.save_overlay(updated, overlay_file)
    return ApplyOutcome(True, "applied" if openclaw_payload.get("inverse") else "recorded (no references to repoint)",
                        updated, rendered=[a for a in res.payloads if a != "openclaw"])


def apply_changes(doc: dict, changes: dict[str, tuple[str, str]], *, apply_patch: ApplyPatch,
                  overlay: dict, overlay_file: Path | None = None,
                  now: Callable[[], datetime] | None = None, artifacts: list | None = None,
                  selection=None) -> tuple[bool, str, dict]:
    """`apply_changes_ex` as the (ok, message, overlay) triple the verbs and tests use."""
    out = apply_changes_ex(doc, changes, apply_patch=apply_patch, overlay=overlay, overlay_file=overlay_file,
                           now=now, artifacts=artifacts, selection=selection)
    return out.ok, out.message, out.overlay


def rollback_last(*, apply_patch: ApplyPatch, overlay: dict, overlay_file: Path | None = None,
                  now: Callable[[], datetime] | None = None, artifacts: list | None = None,
                  selection=None) -> tuple[bool, str, dict]:
    """Restore the recorded change in reverse artifact order (the inverse patch for openclaw.json,
    dry run first), then restore the previous overlay pins."""
    last = (overlay.get("state") or {}).get("last_change") if overlay else None
    if not isinstance(last, dict) or "inverse" not in last:
        return False, "nothing to roll back (no recorded change)", overlay
    payloads = last.get("artifacts")
    if not isinstance(payloads, dict) or "openclaw" not in payloads:
        payloads = {"openclaw": {"inverse": last["inverse"]}, **(payloads or {})}     # recorded before the registry
    ok, msg = model_fanout.restore_all(registry(apply_patch, artifacts), payloads,
                                       model_fanout.Ctx(selection=selection))
    if not ok:
        return False, msg, overlay
    updated = model_pins.migrate(overlay)
    pins = updated.setdefault("pins", {})
    for slot, prev in (last.get("previous_pins") or {}).items():
        slot = model_pins.slot_key(slot)
        if prev:
            pins[slot] = prev
        else:
            pins.pop(slot, None)
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


# --- smoke dispatch: one real call per path ----------------------------------------------------
# Every probe is a billable call on the operator's account. The key travels only in a header (or the
# Claude CLI's own login) and every excerpt is scrubbed of it.

GOOGLE_HOST = "https://generativelanguage.googleapis.com"
OPENROUTER_CHAT = "https://openrouter.ai/api/v1/chat/completions"
OLLAMA_GENERATE = "http://127.0.0.1:11434/api/generate"
ANTHROPIC_MESSAGES = "https://api.anthropic.com/v1/messages"
_PROMPT = "Reply with exactly OK"
# Vendor chat-completions paths (the host is the adapter's constant).
_CHAT_PATH = {"openai": "/v1/chat/completions", "deepseek": "/chat/completions", "moonshot": "/v1/chat/completions"}


def _scrub(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return redact(text)


def _chat_body(model: str) -> dict:
    return {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": _PROMPT}]}


def _judge_chat(status: int, text: str, requested: str, secrets: tuple, *, strict_model: bool = True) -> SmokeResult:
    """Pass only on HTTP 200 with an OK reply from the model that was asked for."""
    if status != 200:
        return SmokeResult(False, f"HTTP {status}", _scrub(text, *secrets))
    try:
        doc = json.loads(text)
        reply = doc["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return SmokeResult(False, "reply was not a chat completion", _scrub(text, *secrets))
    if "OK" not in str(reply):
        return SmokeResult(False, "reply did not contain OK", _scrub(str(reply), *secrets))
    served = str(doc.get("model", ""))
    if strict_model and served and served.split("/")[-1] != requested.split("/")[-1]:
        return SmokeResult(False, f"served model {served!r} is not {requested!r}", _scrub(str(reply), *secrets))
    return SmokeResult(True, "ok", _scrub(str(reply), *secrets))


def smoke_slot(provider: str, model_id: str, path: str, *, http: Callable | None, key: str = "",
               gateway_url: str = "http://127.0.0.1:4000", gateway_key: str = "") -> SmokeResult:
    """The probe for `model_id` of `provider` over `path` (litellm | openrouter | direct)."""
    adapter = model_providers.REGISTRY.get(provider)
    if adapter is None or path not in adapter.smoke_kinds:
        return SmokeResult(False, "no smoke path")
    if http is None:
        return SmokeResult(False, "no HTTP client")
    timeout = SMOKE_TIMEOUT
    if path == "litellm":
        name = model_id if provider == "anthropic" else f"{provider}/{model_id}"
        status, text = http("POST", gateway_url.rstrip("/") + "/v1/chat/completions",
                            {"Authorization": f"Bearer {gateway_key}", "Content-Type": "application/json"},
                            timeout, _chat_body(name))
        return _judge_chat(status, text, name, (gateway_key,))
    if path == "openrouter":
        ns_id = model_pins.openrouter_id(model_id) if provider == "anthropic" else f"{adapter.openrouter_ns}/{model_id}"
        if provider != "anthropic" and not adapter.openrouter_ns:
            return SmokeResult(False, "no smoke path")
        status, text = http("POST", OPENROUTER_CHAT, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                            timeout, _chat_body(ns_id))
        return _judge_chat(status, text, ns_id, (key,))
    # direct
    if provider == "google":
        url = f"{GOOGLE_HOST}/v1beta/models/{model_id}:generateContent"
        status, text = http("POST", url, {"x-goog-api-key": key, "Content-Type": "application/json"}, timeout,
                            {"contents": [{"parts": [{"text": _PROMPT}]}], "generationConfig": {"maxOutputTokens": 16}})
        if status != 200:
            return SmokeResult(False, f"HTTP {status}", _scrub(text, key))
        try:
            reply = json.loads(text)["candidates"][0]["content"]["parts"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError):
            return SmokeResult(False, "reply was not a generateContent response", _scrub(text, key))
        return SmokeResult("OK" in reply, "ok" if "OK" in reply else "reply did not contain OK", _scrub(reply, key))
    if provider == "anthropic":
        status, text = http("POST", ANTHROPIC_MESSAGES, {"x-api-key": key, "anthropic-version": "2023-06-01",
                                                          "Content-Type": "application/json"}, timeout, _chat_body(model_id))
        if status != 200:
            return SmokeResult(False, f"HTTP {status}", _scrub(text, key))
        try:
            doc = json.loads(text)
            reply = doc["content"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError):
            return SmokeResult(False, "reply was not a message", _scrub(text, key))
        if "OK" not in reply or str(doc.get("model", model_id)) != model_id:
            return SmokeResult(False, "reply did not contain OK or the served model differs", _scrub(reply, key))
        return SmokeResult(True, "ok", _scrub(reply, key))
    if provider == "ollama":
        status, text = http("POST", OLLAMA_GENERATE, {"Content-Type": "application/json"}, timeout,
                            {"model": model_id, "prompt": _PROMPT, "stream": False})
        if status != 200:
            return SmokeResult(False, f"HTTP {status}", redact(text))
        try:
            reply = json.loads(text).get("response", "")
        except ValueError:
            return SmokeResult(False, "reply was not JSON", redact(text))
        return SmokeResult("OK" in reply, "ok" if "OK" in reply else "reply did not contain OK", redact(reply))
    chat = _CHAT_PATH.get(provider)
    if not chat or not adapter.vendor_host:
        return SmokeResult(False, "no smoke path")
    status, text = http("POST", adapter.vendor_host + chat,
                        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout, _chat_body(model_id))
    return _judge_chat(status, text, model_id, (key,))


def smoke_path_problem(provider: str, selection, accounts: dict | None) -> str:
    """Why this provider cannot be probed on the selection's path, or "" when it can."""
    adapter = model_providers.REGISTRY.get(provider)
    path = getattr(selection, "smoke_path", "claude-cli") if selection is not None else "claude-cli"
    if adapter is None or path not in adapter.smoke_kinds:
        return "no smoke path"
    acc = (accounts or {}).get(provider)
    if path == "direct" and acc is not None and not getattr(acc, "direct_key", False) and provider != "ollama":
        return "no smoke path"            # an OpenClaw-only credential cannot be used for a direct probe
    return ""


def smoke_proposal(deps: "Deps", provider: str, model_id: str, selection, accounts: dict | None = None) -> SmokeResult:
    """Dispatch one probe. Claude over the Claude CLI keeps the original `deps.smoke`."""
    path = getattr(selection, "smoke_path", "claude-cli") if selection is not None else "claude-cli"
    if provider == "anthropic" and path == "claude-cli":
        return deps.smoke(model_id)
    problem = smoke_path_problem(provider, selection, accounts)
    if problem:
        return SmokeResult(False, problem)
    gateway_url, gateway_key = deps.gateway()
    key = deps.key_for("openrouter" if path == "openrouter" else provider)
    return smoke_slot(provider, model_id, path, http=deps.http, key=key, gateway_url=gateway_url, gateway_key=gateway_key)


@dataclass
class Options:
    check: bool = False
    dry_run: bool = False
    classes: list[str] | None = None
    no_restart: bool = False
    refresh: bool = True
    unattended: bool = False
    apply_proposals: bool = False      # scripted: approve and apply every pending proposal, no prompt


@dataclass
class Result:
    rc: int
    outcome: str
    message: str = ""
    proposals: list[Proposal] = field(default_factory=list)
    applied: dict[str, tuple[str, str]] = field(default_factory=dict)
    deferrals: int = 0
    providers: list = field(default_factory=list)       # ProviderRow per provider discovery asked about
    rendered: list = field(default_factory=list)        # artifact ids re-rendered besides the overlay and openclaw.json

    def as_dict(self) -> dict:
        return {"rc": self.rc, "outcome": self.outcome, "message": self.message, "deferrals": self.deferrals,
                "providers": [r.as_dict() for r in self.providers], "rerendered": list(self.rendered),
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
    # Provider-agnostic seams; the defaults keep a Claude-only host exactly as it was.
    selection: Callable[[], object] = lambda: None
    accounts: Callable[[], dict] = lambda: {}
    http: Callable | None = None
    key_for: Callable[[str], str] = lambda provider: ""
    gateway: Callable[[], tuple[str, str]] = lambda: ("http://127.0.0.1:4000", "")
    # Extra artifacts of the fan-out registry (S12 registers the re-rendered files here).
    artifacts: Callable[[], list] = lambda: []
    route_plan: Callable[[], dict] = lambda: {}        # cockpit -> CockpitAction, from the compatibility matrix


def _load(deps: Deps) -> dict:
    ov = model_pins.load_overlay(deps.overlay_file)
    return ov or model_pins.empty_overlay()


def _persist(deps: Deps, ov: dict, **state_fields) -> dict:
    ov.setdefault("state", {}).update(state_fields)
    model_pins.save_overlay(ov, deps.overlay_file)
    return ov


def _log(deps: Deps, event: str, **kw) -> None:
    deps.log({"at": deps.now().isoformat(), "event": event, **kw})


PRICE_TTL_HOURS = 24
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"


def openrouter_price_map(raw: dict) -> dict[str, list[float]]:
    """bare model id -> [input, output] USD per 1M tokens, from OpenRouter's public `/models` JSON."""
    out: dict[str, list[float]] = {}
    for entry in raw.get("data", []) if isinstance(raw, dict) else []:
        try:
            ns, bare = str(entry["id"]).split("/", 1)
            pricing = entry["pricing"]
            price = [round(float(pricing["prompt"]) * 1e6, 6), round(float(pricing["completion"]) * 1e6, 6)]
        except (KeyError, ValueError, TypeError):
            continue
        for adapter in model_providers.REGISTRY.values():
            if adapter.openrouter_ns == ns:
                ref = adapter.from_any_spelling(bare)
                out[bare.replace(".", "-") if adapter.id == "anthropic" else bare] = price
                if ref:
                    out.setdefault(bare, price)
    return out


def _with_prices(deps: "Deps", ov: dict, selection) -> dict:
    """The overlay with OpenRouter's public prices folded under the operator's own `prices` (Q4).
    Only for an openrouter-backend selection; cached 24 h with a timestamp; never invented."""
    if selection is None or getattr(selection, "smoke_path", "") != "openrouter" or deps.http is None:
        return ov
    cache = ov.get("price_cache") if isinstance(ov.get("price_cache"), dict) else {}
    now = deps.now()
    fresh = False
    try:
        fresh = now - datetime.fromisoformat(cache.get("fetched_at", "")) < timedelta(hours=PRICE_TTL_HOURS)
    except ValueError:
        pass
    prices = cache.get("prices") if fresh and isinstance(cache.get("prices"), dict) else None
    if prices is None:
        try:
            status, text = deps.http("GET", OPENROUTER_MODELS, {"Accept": "application/json"}, 30)
            prices = openrouter_price_map(json.loads(text)) if status == 200 else {}
        except Exception:  # noqa: BLE001
            prices = {}
        if prices:
            ov["price_cache"] = {"fetched_at": now.isoformat(), "prices": prices}
    merged = dict(ov)
    merged["prices"] = {**prices, **(ov.get("prices") if isinstance(ov.get("prices"), dict) else {})}
    return merged


def _catalog_missing(effective: dict, disc: "Discovery") -> list[str]:
    """Slots whose effective id is absent from a catalog that answered (doctor reports them)."""
    ok = {r.provider for r in disc.providers if r.status == "ok"}
    out = []
    for key, model_id in effective.items():
        slot = model_pins.slot_key(key)
        provider = model_pins.slot_provider(slot)
        if provider in ok and model_id and model_id not in disc.listed.get(provider, set()):
            out.append(slot)
    return sorted(out)


def approve_command(slot: str, model_id: str) -> str:
    """The exact command that approves one proposal. Claude classes keep the short form."""
    slot = model_pins.slot_key(slot)
    if model_pins.slot_provider(slot) == model_pins.PROVIDER_ANTHROPIC:
        return f"ai-resources models approve {model_pins.slot_family(slot)} {model_id}"
    return f"ai-resources models approve --slot {slot} {model_id}"


def _wanted(opts: Options) -> set[str]:
    """The slots a caller asked about (`--class sonnet` is the alias of `--slot anthropic:sonnet`)."""
    return {model_pins.slot_key(c) for c in (opts.classes or [])}


@dataclass
class Plan:
    """One discovery, as the user saw it: the interactive path applies THIS result, never a second one."""
    disc: "Discovery"
    props: list
    revision: str
    selection: object = None
    accounts: dict | None = None


def _discover_for(deps: Deps, opts: Options, now: datetime, ctx: dict | None):
    """(selection, accounts, discovery) or a Result for a failed discovery."""
    selection = deps.selection()
    accounts = deps.accounts() if selection is not None else None
    disc = discover(deps.runner, refresh=opts.refresh, warn=lambda m: _log(deps, "warn", message=m),
                    selection=selection, accounts=accounts, http=deps.http, key_for=deps.key_for,
                    monotonic=deps.monotonic)
    if ctx is not None:
        ctx["providers"] = disc.providers
    return selection, accounts, disc


def make_plan(opts: Options, deps: Deps) -> "Plan | Result":
    """Take the lock, discover ONCE, propose, record the overlay revision, release the lock.

    The interactive path calls this, asks, then calls `run_update(..., plan=plan)`: the lock is not
    held while a human reads a prompt."""
    with deps.lock() as got:
        if not got:
            return Result(EXIT_LOCKED, "locked", "another models update is running")
        ov = _load(deps)
        ctx: dict = {}
        try:
            selection, accounts, disc = _discover_for(deps, opts, deps.now(), ctx)
        except DiscoveryError as e:
            _log(deps, "discovery_failed", error=str(e))
            return Result(EXIT_ERROR, "error", str(e), providers=ctx.get("providers", []))
        effective = model_pins.effective_slots(selection, ov) if selection is not None else model_pins.effective(ov)
        props = propose(effective, disc, _with_prices(deps, ov, selection), selection, accounts)
        return Plan(disc, props, models_interaction.revision(ov), selection, accounts)


def run_update(opts: Options, deps: Deps, *, plan: "Plan | None" = None, answers: dict | None = None,
               expect_revision: str | None = None) -> Result:
    """Discover, propose and apply. With a `plan` (the interactive path) no second discovery runs and
    the overlay must still be at `expect_revision`; `answers` are the user's replies to the plan."""
    with deps.lock() as got:
        if not got:
            return Result(EXIT_LOCKED, "locked", "another models update is running")
        ctx: dict = {}
        result = _run_locked(opts, deps, ctx, plan, answers, expect_revision)
        result.providers = ctx.get("providers", [])
        result.rendered = ctx.get("rendered", [])
        return result


def _run_locked(opts: Options, deps: Deps, ctx: dict | None = None, plan: "Plan | None" = None,
                answers: dict | None = None, expect_revision: str | None = None) -> Result:
    writes = not (opts.check or opts.dry_run)
    ov = _load(deps)
    st = ov.get("state") or {}
    now = deps.now()

    if st.get("pending_restart") and writes:
        last = st.get("last_change") or {}
        applied = {c: tuple(v) for c, v in (last.get("changes") or {}).items()}
        _log(deps, "resume_restart", applied=list(applied))
        return _restart_and_verify(opts, deps, ov, applied, resume=True)

    if expect_revision is not None and models_interaction.revision(ov) != expect_revision:
        # The policy moved while a human was reading the prompt: do not apply an answer to a
        # question that no longer matches. Nothing was written.
        _log(deps, "overlay_changed")
        return Result(EXIT_ERROR, "overlay_changed",
                      "the model policy changed while you were answering; nothing was written; run the command again")

    explicit = bool(answers) or opts.apply_proposals            # a human asked for this run: no cooldown
    cooldown = policy_of(ov)["cooldown_hours"]
    last_switch = st.get("last_switch_at")
    if writes and last_switch and not explicit:
        try:
            if now - datetime.fromisoformat(last_switch) < timedelta(hours=cooldown):
                _log(deps, "cooldown", last_switch_at=last_switch)
                return Result(EXIT_OK, "cooldown", f"switched less than {cooldown:g} h ago")
        except ValueError:
            pass

    if answers and writes:
        ov = models_interaction.apply_answers(ov, answers)
        model_pins.save_overlay(ov, deps.overlay_file)

    if plan is not None:
        selection, accounts, disc = plan.selection, plan.accounts, plan.disc       # the SAME discovery the user answered
        if ctx is not None:
            ctx["providers"] = disc.providers
    else:
        try:
            selection, accounts, disc = _discover_for(deps, opts, now, ctx)
        except DiscoveryError as e:
            _log(deps, "discovery_failed", error=str(e))
            if writes:
                _persist(deps, ov, last_run=now.isoformat(), last_result="error")
            return Result(EXIT_ERROR, "error", str(e), providers=(ctx or {}).get("providers", []))
    effective = model_pins.effective_slots(selection, ov) if selection is not None else model_pins.effective(ov)
    props = propose(effective, disc, _with_prices(deps, ov, selection), selection, accounts)
    if opts.apply_proposals and writes:
        # Scripted: every proposal waiting for a human is approved with the exact id found.
        for p in props:
            if p.decision == "needs_approval":
                ov.setdefault("approvals", {})[p.slot] = p.new
        props = propose(effective, disc, _with_prices(deps, ov, selection), selection, accounts)
    needs = [p for p in props if p.decision == "needs_approval"]
    if writes:
        pending = ov.setdefault("pending", {})
        for slot in list(pending):
            if slot not in {p.slot for p in needs}:
                pending.pop(slot)
        for p in needs:
            first = (pending.get(p.slot) or {}).get("first_seen") if (pending.get(p.slot) or {}).get("to") == p.new else None
            pending[p.slot] = {"to": p.new, "reason": ", ".join(p.reasons), "first_seen": first or now.isoformat()}
    if writes:
        ov.setdefault("state", {})["catalog_missing"] = _catalog_missing(effective, disc)
    wanted = _wanted(opts)
    applicable = [p for p in props if p.applicable and (not wanted or p.slot in wanted)]
    _log(deps, "plan", proposals=[p.as_dict() for p in props])

    if wanted:
        needs = [p for p in needs if p.slot in wanted]            # only what the caller asked about
    if not applicable:
        rc = EXIT_APPROVAL_PENDING if needs else EXIT_OK
        if writes:
            _persist(deps, ov, last_run=now.isoformat(), last_result="pending_approval" if needs else "no_change")
        return Result(rc, "pending_approval" if needs else "no_change", proposals=props)
    if opts.check:
        return Result(EXIT_APPLICABLE, "applicable", "changes are ready to apply", proposals=props)

    survivors: list[Proposal] = []
    for p in applicable:
        res = smoke_proposal(deps, p.provider, p.new, selection, accounts)
        _log(deps, "smoke", model=p.new, ok=res.ok, reason=res.reason, excerpt=res.excerpt)
        if res.ok:
            survivors.append(p)
    if not survivors:
        if writes:
            _persist(deps, ov, last_run=now.isoformat(), last_result="smoke_failed")
        return Result(EXIT_PRECHECK_FAILED, "smoke_failed", "smoke failed for every candidate", proposals=props)
    changes = {p.slot: (p.old, p.new) for p in survivors}

    if deps.busy():
        if opts.dry_run:
            return Result(EXIT_DEFERRED, "deferred", "the gateway is busy", proposals=props)
        count = int(st.get("deferrals", 0)) + 1
        _persist(deps, ov, last_run=now.isoformat(), last_result="deferred", deferrals=count)
        _log(deps, "deferred", reason="gateway busy")
        return Result(EXIT_DEFERRED, "deferred", "the gateway is busy; nothing was patched", proposals=props,
                      deferrals=count)

    doc = deps.read_config()
    if opts.dry_run:
        built = build_forward_patch(doc, {model_pins.openclaw_ref(o): model_pins.openclaw_ref(n)
                                          for o, n in changes.values()})
        if built["patch"]:
            ok, out = deps.apply_patch(built["patch"], dry_run=True, replace_paths=[])
            if not ok:
                return Result(EXIT_PRECHECK_FAILED, "patch_rejected", out[-300:], proposals=props)
        return Result(EXIT_OK, "dry_run", "the patch validates; nothing was applied", proposals=props, applied=changes)

    backup_dir = backup(deps.config_file(), overlay_file=deps.overlay_file, root=deps.backup_root, now=deps.now)
    outcome = apply_changes_ex(doc, changes, apply_patch=deps.apply_patch, overlay=ov,
                               overlay_file=deps.overlay_file, now=deps.now, artifacts=deps.artifacts(),
                               selection=selection, plan=deps.route_plan(), backup_dir=backup_dir)
    if ctx is not None:
        ctx["rendered"] = outcome.rendered
    ok, msg, ov = outcome.ok, outcome.message, outcome.overlay
    if not ok:
        if outcome.restore_failed:
            _persist(deps, ov, last_run=now.isoformat(), last_result="rollback_failed")
            _log(deps, "rollback_failed", artifact=outcome.failed, message=msg)
            return Result(EXIT_ROLLBACK_FAILED, "rollback_failed", f"{outcome.failed}: {msg}", proposals=props)
        if outcome.unwound:          # a later artifact failed after an earlier one had been written
            _persist(deps, ov, last_run=now.isoformat(), last_result="rolled_back")
            _log(deps, "apply_unwound", artifact=outcome.failed, message=msg)
            return Result(EXIT_ROLLED_BACK, "rolled_back", f"{outcome.failed}: {msg}; earlier artifacts restored",
                          proposals=props)
        _persist(deps, ov, last_run=now.isoformat(), last_result="patch_rejected")
        _log(deps, "apply_failed", message=msg)
        return Result(EXIT_PRECHECK_FAILED, "patch_rejected", msg, proposals=props)
    _log(deps, "applied", changes={c: list(v) for c, v in changes.items()})
    if opts.no_restart:
        _persist(deps, ov, last_run=now.isoformat(), last_result="switched", last_switch_at=now.isoformat())
        return Result(EXIT_OK, "switched", "switched; the gateway was not restarted (--no-restart)",
                      proposals=props, applied=changes)
    if not any(slot_ref(slot, old) for slot, (old, _new) in changes.items()):
        # Only slots OpenClaw has no verified runtime for moved: openclaw.json did not change, so
        # there is nothing to restart or health-check.
        _persist(deps, ov, last_run=now.isoformat(), last_result="switched", last_switch_at=now.isoformat())
        return Result(EXIT_OK, "switched", "recorded; openclaw.json has no reference to repoint",
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
                count = int(st.get("deferrals", 0)) + 1
                _persist(deps, ov, pending_restart=True, pending_restart_pid=pid_before, last_run=now.isoformat(),
                         last_result="deferred", deferrals=count)
                _log(deps, "restart_deferred", reason=e.reason)
                return Result(EXIT_DEFERRED, "deferred", f"restart deferred: {e.reason}", proposals, applied,
                              deferrals=count)
        healthy = _await_health(deps)
        pid_changed = already or not pid_before or deps.main_pid() != pid_before
        reason = "" if healthy and pid_changed else ("gateway unhealthy after restart" if not healthy
                                                     else "gateway pid did not change")
        if not reason:
            selection_now = deps.selection()
            for slot, (_old, new) in applied.items():
                res = smoke_proposal(deps, model_pins.slot_provider(slot), new, selection_now,
                                     deps.accounts() if selection_now is not None else None)
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
                                      now=deps.now, artifacts=deps.artifacts(), selection=deps.selection())
        if not ok:
            _persist(deps, ov, last_run=now.isoformat(), last_result="rollback_failed")
            _log(deps, "rollback_failed", message=msg)
            return Result(EXIT_ROLLBACK_FAILED, "rollback_failed", f"{reason}; {msg}", proposals, applied)
        back["state"].pop("pending_restart", None)
        back["state"].pop("pending_restart_pid", None)
        failed = back["state"].setdefault("failed", {})
        for slot, (_old, new) in applied.items():
            slot = model_pins.slot_key(slot)
            failed[slot] = new
            # the approval is spent by the failed attempt; re-approving (which clears `failed`) retries
            if (back.get("approvals") or {}).get(slot) == new:
                back["approvals"].pop(slot, None)
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


def restart_backend(selection) -> str:
    """The backend whose registration a restart waits for: the Claude CLI unless the selection has
    no Claude slot, then the runtime of the first slot that has one."""
    if selection is None or any(model_pins.slot_provider(s) == "anthropic" for s in selection.slots):
        return "claude-cli"
    for slot in selection.slots:
        adapter = model_providers.REGISTRY.get(model_pins.slot_provider(slot))
        if adapter and adapter.runtime:
            return adapter.runtime
    return "claude-cli"


def default_deps() -> Deps:
    """The real wiring. Imports the OpenClaw cockpit lazily: it is heavy and the pure functions
    above must stay importable without it."""
    from .setup.cockpits import openclaw as oc
    runner = openclaw_host.default_runner

    def restart() -> object:
        try:
            return oc.restart_gateway(backend=restart_backend(selection()), ask=None, interactive=False,
                                      restart_timeout=RESTART_TIMEOUT)
        except oc.RestartDeferred as e:
            raise RestartDeferred(e.reason) from None

    def health() -> bool:
        rc, _ = runner([openclaw_host.resolve_openclaw_bin(), "health"], timeout=60)
        return rc == 0

    from . import model_accounts
    from .setup import credentials, state as setup_state

    def selection():
        try:
            return setup_state.load().get_selection()
        except Exception:  # noqa: BLE001 - a broken state file must not stop the Claude-only path
            return None

    def key_for(provider: str) -> str:
        if provider == "openrouter":
            return credentials.get_key("OPENROUTER_API_KEY")
        adapter = model_providers.REGISTRY.get(provider)
        return credentials.get_key(adapter.credential[0]) if adapter and adapter.credential else ""

    def host_artifacts() -> list:
        from . import model_rerender
        try:
            st = setup_state.load()
            return model_rerender.build(state=st, selection=st.get_selection(),
                                        detected=[c for c, cs in st.cockpits.items() if cs.installed])
        except Exception:  # noqa: BLE001 - a broken state file must not stop the OpenClaw update
            return []

    def route_plan() -> dict:
        from .setup import model_selection
        try:
            st = setup_state.load()
            sel = st.get_selection()
            detected = [c for c, cs in st.cockpits.items() if cs.installed]
            return {a.cockpit: a for a in model_selection.plan_table(sel, st.mode, st.backend, detected)}
        except Exception:  # noqa: BLE001
            return {}

    def gateway() -> tuple[str, str]:
        st = setup_state.load()
        host = f"http://{st.litellm.local.bind_address}:{st.litellm.local.port}"
        return host, credentials.get_key("LITELLM_MASTER_KEY")

    return Deps(
        runner=runner, apply_patch=oc.apply_patch, read_config=lambda: oc.read_config(oc.config_path()),
        config_file=oc.config_path, smoke=lambda m: smoke(m, runner, claude_bin=shutil.which("claude") or "claude"),
        busy=lambda: openclaw_host.gateway_busy()[0], restart=restart, health=health,
        main_pid=openclaw_host.gateway_main_pid, log=_append_log,
        selection=selection, accounts=lambda: model_accounts.detect(model_accounts.default_deps(runner)),
        http=make_http(), key_for=key_for, gateway=gateway, artifacts=host_artifacts, route_plan=route_plan)


def run_rollback(deps: Deps) -> Result:
    """`models rollback`: the recorded inverse patch, then the same restart and health flow."""
    with deps.lock() as got:
        if not got:
            return Result(EXIT_LOCKED, "locked", "another models update is running")
        ov = _load(deps)
        ok, msg, back = rollback_last(apply_patch=deps.apply_patch, overlay=ov, overlay_file=deps.overlay_file,
                                      now=deps.now, artifacts=deps.artifacts(), selection=deps.selection())
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


# --- findings for doctor, verify and status ----------------------------------------------------

def _selection_findings(overlay: dict, selection, accounts: dict | None) -> list[tuple[str, str, str]]:
    if selection is None:
        return []
    out: list[tuple[str, str, str]] = []
    if accounts:
        for pid in selection.enabled_providers():
            acc = accounts.get(pid)
            if acc is not None and not getattr(acc, "credentialed", True):
                out.append(("warn", f"provider {pid} is enabled but has lost its credentials",
                            f"add the key (`ai-resources setup`) or disable {pid}"))
    st = overlay.get("state") or {}
    for slot in st.get("catalog_missing") or []:
        out.append(("warn", f"{slot}: the running model id is no longer listed by its catalog",
                    "`ai-resources models check --refresh`; pin or approve a listed id"))
    eff = model_pins.effective_slots(selection, overlay)
    for slot, model_id in eff.items():
        adapter = model_providers.REGISTRY.get(model_pins.slot_provider(slot))
        ref = adapter.from_any_spelling(model_id) if adapter else None
        if ref and ref.channel in ("preview", "alias"):
            out.append(("warn", f"{slot} runs {model_id}, a {ref.channel} id that never updates on its own",
                        f"`ai-resources models pin --slot {slot} <stable id>` or approve a stable one"))
    return out


TIMER_UNIT = "openclaw-models-update.timer"
STALE_RUN_DAYS = 3


def model_findings(report: dict, overlay: dict, *, now: datetime | None = None, selection=None,
                   accounts: dict | None = None) -> list[tuple[str, str, str]]:
    """(level, message, remedy) for the host report: drift, pending approvals and the last run.

    With a `selection` it also flags an enabled provider that lost its credentials (needs
    `accounts`), a slot whose id the catalog no longer lists, and a slot pinned to a preview or an
    alias. Levels are the verify levels: ok (advisory text), warn, error."""
    now = now or datetime.now(timezone.utc)
    out: list[tuple[str, str, str]] = []
    drift = [m for m in report.get("models", []) if m.get("drift")]
    if drift:
        rows = ", ".join(f"{m['agent']} ({m['model']} -> {m['drift']})" for m in drift)
        out.append(("warn", f"{len(drift)} model reference(s) in openclaw.json lag the pins: {rows}",
                    "`ai-resources models status`; `ai-resources setup` re-applies the canonical config"))
    overlay = model_pins.migrate(overlay) if overlay else {}
    pending = overlay.get("pending") or {}
    if pending:
        cmds = "; ".join(approve_command(c, p.get("to")) for c, p in pending.items())
        out.append(("ok", f"{len(pending)} model upgrade(s) await approval", cmds))
    st = overlay.get("state") or {}
    out += _selection_findings(overlay, selection, accounts)
    result = st.get("last_result")
    if result == "rollback_failed":
        out.append(("error", "the last models update could not be rolled back",
                    "check `openclaw health` and `ai-resources models status` now"))
    elif result == "rolled_back":
        out.append(("warn", "the last models update was rolled back",
                    "see ~/.openclaw/logs/models-update.log; `ai-resources models approve` retries it"))
    timer_on = any(t.get("name") == TIMER_UNIT and t.get("enabled") for t in report.get("timers", []))
    last_run = st.get("last_run")
    if timer_on and last_run:
        try:
            if now - datetime.fromisoformat(last_run) > timedelta(days=STALE_RUN_DAYS):
                out.append(("warn", f"the models update has not run for more than {STALE_RUN_DAYS} days",
                            f"systemctl --user status {TIMER_UNIT}"))
        except ValueError:
            pass
    return out
