"""The single declaration of the Claude model pins used on the OpenClaw path.

Each class (opus, sonnet, haiku, fable) has ONE kit default here. A host overlay at
`~/.config/ai-resources/model-pins.json` (written by `ai-resources models ...`) can move a class
forward without waiting for a kit release. The effective pin is the HIGHER of the kit default and
the overlay, so a stale overlay never holds a host behind a newer kit; the only way to hold a class
down is to freeze it with an explicit pin.

This module has no import-time side effects and imports nothing from `setup`, so `setup.state`
and the cockpits can import it freely.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

CLASSES = ("opus", "sonnet", "haiku", "fable")
SCHEMA = 2
PROVIDER_ANTHROPIC = "anthropic"

# Per-slot answer to "may a newer model of this slot move on its own?" (overlay schema 2).
ANSWERS = ("always", "ask", "never")
MAX_BUMPS = ("minor", "any")

# Kit defaults: dashed Anthropic ids. Bump these in a kit release.
DEFAULTS: dict[str, str] = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
    "fable": "claude-fable-5-1",
}

# Antigravity (agy) model ids. `agy models` is not trusted for unattended upgrades, so this list is
# edited by hand; the first entry is the default. Moved verbatim from ENGINES["antigravity"].
AGY_STATIC: tuple[str, ...] = (
    "gemini-3.8-flash-low", "gemini-3.8-flash-high", "gemini-3.1-pro-low",
    "claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium",
)

_ID_RE = re.compile(r"^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d{1,2}))?$")


def parse_id(model_id: str) -> tuple[str, int, int] | None:
    """`claude-sonnet-5-5` -> ("sonnet", 5, 5); `claude-opus-5` -> ("opus", 5, 0).

    Dated forms (`-20251001`), `-thinking` forms, context suffixes and families outside the four
    classes (for example `claude-mythos-5`) return None.
    """
    if not isinstance(model_id, str):
        return None
    m = _ID_RE.match(model_id.strip())
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3) or 0)


def openclaw_ref(model_id: str) -> str:
    """The OpenClaw model ref for a Claude id (`anthropic/<id>`); confirmed valid by the S1 spike."""
    return "anthropic/" + model_id


def openrouter_id(model_id: str) -> str:
    """`claude-haiku-4-5` -> `anthropic/claude-haiku-4.5` (OpenRouter's dotted minor)."""
    m = _ID_RE.match(model_id.strip())
    if not m:
        return "anthropic/" + model_id
    family, major, minor = m.group(1), m.group(2), m.group(3)
    return f"anthropic/claude-{family}-{major}" + (f".{minor}" if minor is not None else "")


# --- overlay ------------------------------------------------------------------------------------

def overlay_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "ai-resources" / "model-pins.json"


def empty_overlay() -> dict:
    return {"schema": SCHEMA, "pins": {}, "policy": {"slots": {}, "families": {}, "max_cost_delta_pct": 0,
                                                      "exclude": [], "cooldown_hours": 6},
            "approvals": {}, "pending": {}, "state": {}, "history": []}


# --- slots --------------------------------------------------------------------------------------
# A slot is "<provider>:<family>". The four Claude classes are the anthropic slots; a bare class
# name ("sonnet") is accepted everywhere and normalised to "anthropic:sonnet".

def slot_key(name: str) -> str:
    if ":" in name:
        return name
    return f"{PROVIDER_ANTHROPIC}:{name}" if name in CLASSES else name


def slot_provider(slot: str) -> str:
    return slot_key(slot).split(":", 1)[0]


def slot_family(slot: str) -> str:
    return slot_key(slot).split(":", 1)[1] if ":" in slot_key(slot) else slot


def anthropic_slots() -> list[str]:
    return [slot_key(c) for c in CLASSES]


def _rekey(d, *, nested_values: bool = False):
    if not isinstance(d, dict):
        return d
    return {slot_key(k): v for k, v in d.items()}


_V1_MODE_TO_POLICY = {
    "auto-minor": {"answer": "always", "max_bump": "minor"},
    "approve": {"answer": "ask"},
    "frozen": {"answer": "ask", "frozen": True},
}


def migrate(overlay: dict) -> dict:
    """A schema-2 copy of `overlay`. Idempotent: a v2 overlay (or a half-migrated one) is unchanged.

    v1 keyed everything by Claude class and carried `policy.classes[cls].mode`; v2 keys by slot and
    carries `policy.slots[slot] = {answer, max_bump, frozen, never_ids, pending}`.
    """
    import copy
    if not isinstance(overlay, dict):
        return overlay
    out = copy.deepcopy(overlay)
    for key in ("pins", "approvals", "pending"):
        if isinstance(out.get(key), dict):
            out[key] = _rekey(out[key])
    state = out.get("state")
    if isinstance(state, dict):
        if isinstance(state.get("failed"), dict):
            state["failed"] = _rekey(state["failed"])
        last = state.get("last_change")
        if isinstance(last, dict):
            for key in ("changes", "previous_pins"):
                if isinstance(last.get(key), dict):
                    last[key] = _rekey(last[key])
    policy = out.get("policy")
    if isinstance(policy, dict):
        classes = policy.pop("classes", None)
        slots = policy.get("slots") if isinstance(policy.get("slots"), dict) else {}
        slots = _rekey(slots)
        if isinstance(classes, dict):
            for cls, entry in classes.items():
                if not isinstance(entry, dict):
                    continue
                mapped = dict(_V1_MODE_TO_POLICY.get(entry.get("mode", "auto-minor"), {}))
                slots.setdefault(slot_key(cls), mapped)
        policy["slots"] = slots
        if not isinstance(policy.get("families"), dict):
            policy["families"] = {}
    if "schema" in out or out:
        out["schema"] = SCHEMA
    return out


def default_policy(slot: str) -> dict:
    """The answer a slot has until the user gives one: anthropic slots keep the v1 behaviour
    (apply minor bumps on their own); every other slot asks."""
    if slot_provider(slot) == PROVIDER_ANTHROPIC:
        return {"answer": "always", "max_bump": "minor"}
    return {"answer": "ask", "max_bump": "minor"}


def slot_policy(overlay: dict, slot: str) -> dict:
    """The policy of one slot with defaults filled in. Reads bare class names too."""
    slot = slot_key(slot)
    pol = overlay.get("policy") if isinstance(overlay.get("policy"), dict) else {}
    slots = _rekey(pol.get("slots")) if isinstance(pol.get("slots"), dict) else {}
    legacy = pol.get("classes") if isinstance(pol.get("classes"), dict) else {}
    entry = slots.get(slot)
    if entry is None and isinstance(legacy.get(slot_family(slot)), dict) and slot_provider(slot) == PROVIDER_ANTHROPIC:
        entry = _V1_MODE_TO_POLICY.get(legacy[slot_family(slot)].get("mode", "auto-minor"), {})
    out = default_policy(slot)
    if isinstance(entry, dict):
        out.update(entry)
    out.setdefault("never_ids", [])
    out.setdefault("frozen", False)
    families = pol.get("families") if isinstance(pol.get("families"), dict) else {}
    out["family_never"] = bool((families.get(slot) or {}).get("never")) if isinstance(families.get(slot), dict) else False
    return out


def _backup_v1_once(path: Path) -> None:
    """Copy a schema-1 overlay to `<path>.v1.bak` before the first schema-2 write (kept once)."""
    bak = path.with_name(path.name + ".v1.bak")
    if bak.exists() or not path.exists():
        return
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("schema", 1) != 1:
            return
    except (OSError, ValueError, AttributeError):
        return
    try:
        fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(path.read_text(encoding="utf-8"))
    except OSError as e:
        log.warning("could not keep %s (%s)", bak, e)


def load_overlay(path: Path | None = None) -> dict:
    """The overlay, or {} when missing or malformed. Never raises."""
    path = path or overlay_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        log.warning("model pins overlay %s unreadable (%s); using kit defaults", path, e)
        return {}
    try:
        data = json.loads(raw)
    except ValueError as e:
        log.warning("model pins overlay %s is not valid JSON (%s); using kit defaults", path, e)
        return {}
    if not isinstance(data, dict):
        log.warning("model pins overlay %s is not an object; using kit defaults", path)
        return {}
    return migrate(data)


def save_overlay(data: dict, path: Path | None = None) -> Path:
    """Atomic write (tmp file then os.replace), mode 0600. Keeps the last 50 history entries."""
    path = path or overlay_path()
    data = migrate(dict(data))
    data["schema"] = SCHEMA
    if isinstance(data.get("history"), list):
        data["history"] = data["history"][-50:]
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_v1_once(path)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def _version(model_id: str) -> tuple[int, int] | None:
    p = parse_id(model_id)
    return (p[1], p[2]) if p else None


def is_frozen(overlay: dict, cls: str) -> bool:
    """A slot is frozen when it has a pin and a frozen policy. `cls` may be a class or a slot."""
    slot = slot_key(cls)
    ov = migrate(overlay) if isinstance(overlay, dict) else {}
    pins = ov.get("pins") if isinstance(ov.get("pins"), dict) else {}
    return bool(slot_policy(ov, slot).get("frozen")) and bool(pins.get(slot))


def effective(overlay: dict | None = None) -> dict[str, str]:
    """Per class: the higher of the kit default and the overlay pin; a frozen class keeps its pin."""
    if overlay is None:
        overlay = load_overlay()
    overlay = migrate(overlay)
    pins = overlay.get("pins") if isinstance(overlay.get("pins"), dict) else {}
    out = dict(DEFAULTS)
    for cls in CLASSES:
        pinned = pins.get(slot_key(cls))
        if not isinstance(pinned, str):
            continue
        if is_frozen(overlay, cls):
            out[cls] = pinned
            continue
        pv, dv = _version(pinned), _version(DEFAULTS[cls])
        pp = parse_id(pinned)
        if pv and pp and pp[0] == cls and dv and pv > dv:
            out[cls] = pinned
    return out


def _bare(ref_or_id: str) -> str:
    return ref_or_id.split("/", 1)[1] if "/" in ref_or_id else ref_or_id


def effective_slots(selection=None, overlay: dict | None = None) -> dict[str, str]:
    """slot -> effective bare model id, for the selection's slots (None: the four anthropic slots).

    Anthropic: the higher of the kit default and the overlay pin (`effective()`). Every other
    slot: the highest of the kit default for its family, the selection's own id and the overlay pin,
    unless the slot is frozen (its pin wins) or its track is `fixed` (the selection's id wins).
    """
    from . import model_providers as mpv
    overlay = migrate(load_overlay() if overlay is None else overlay)
    claude = effective(overlay)
    if selection is None:
        return {slot_key(c): claude[c] for c in CLASSES}
    pins = overlay.get("pins") if isinstance(overlay.get("pins"), dict) else {}
    out: dict[str, str] = {}
    for slot, sel in selection.slots.items():
        provider, family = slot.split(":", 1)
        track = sel.get("track", "family") if isinstance(sel, dict) else getattr(sel, "track", "family")
        ref = sel.get("ref", "") if isinstance(sel, dict) else getattr(sel, "ref", "")
        chosen = _bare(ref) if ref else ""
        if provider == PROVIDER_ANTHROPIC and family in claude:
            out[slot] = chosen if (track == "fixed" and chosen) else claude[family]
            continue
        adapter = mpv.REGISTRY.get(provider)
        if is_frozen(overlay, slot):
            out[slot] = pins[slot]
            continue
        if track == "fixed" and chosen:
            out[slot] = chosen
            continue
        cands = [c for c in (chosen, pins.get(slot), adapter.default_id(family) if adapter else None) if c]
        best, best_ver = (chosen or (cands[0] if cands else "")), None
        for cand in cands:
            ref_ = adapter.from_any_spelling(cand) if adapter else None
            if ref_ and ref_.family == family and ref_.channel == "stable" and (best_ver is None or ref_.version > best_ver):
                best, best_ver = cand, ref_.version
        out[slot] = best
    return out


def ref_drift(ref: str, eff: dict[str, str] | None = None, slots: dict[str, str] | None = None) -> str | None:
    """The ref a model reference SHOULD carry, or None when it matches (or is not a managed ref).

    `eff` is the class -> id map for Claude refs (`effective()`); `slots` is the slot -> id map from
    `effective_slots()` for every other provider. Used by status, doctor and verify to flag a config
    that lags the pins."""
    from . import model_providers as mpv
    if not isinstance(ref, str) or "/" not in ref:
        return None
    prefix, bare = ref.split("/", 1)
    if prefix == "anthropic":
        parsed = parse_id(bare)
        if not parsed:
            return None
        wanted = openclaw_ref((eff or effective())[parsed[0]])
        return wanted if wanted != ref else None
    if not slots:
        return None
    adapter = next((a for a in mpv.REGISTRY.values() if a.ref_prefix == prefix and prefix != "anthropic"), None)
    parsed_ref = adapter.from_any_spelling(bare) if adapter else None
    if not parsed_ref or parsed_ref.channel != "stable":
        return None
    want = slots.get(parsed_ref.slot)
    if not want:
        return None
    wanted = f"{prefix}/{want}"
    return wanted if wanted != ref else None
