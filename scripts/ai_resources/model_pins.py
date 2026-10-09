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
SCHEMA = 1

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
    return {"schema": SCHEMA, "pins": {}, "policy": {"classes": {}, "max_cost_delta_pct": 0, "exclude": [],
                                                      "cooldown_hours": 6},
            "approvals": {}, "pending": {}, "state": {}, "history": []}


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
    return data


def save_overlay(data: dict, path: Path | None = None) -> Path:
    """Atomic write (tmp file then os.replace), mode 0600. Keeps the last 50 history entries."""
    path = path or overlay_path()
    data = dict(data)
    data["schema"] = SCHEMA
    if isinstance(data.get("history"), list):
        data["history"] = data["history"][-50:]
    path.parent.mkdir(parents=True, exist_ok=True)
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
    policy = overlay.get("policy") if isinstance(overlay.get("policy"), dict) else {}
    classes = policy.get("classes") if isinstance(policy.get("classes"), dict) else {}
    entry = classes.get(cls) if isinstance(classes.get(cls), dict) else {}
    pins = overlay.get("pins") if isinstance(overlay.get("pins"), dict) else {}
    return entry.get("mode") == "frozen" and bool(pins.get(cls))


def effective(overlay: dict | None = None) -> dict[str, str]:
    """Per class: the higher of the kit default and the overlay pin; a frozen class keeps its pin."""
    if overlay is None:
        overlay = load_overlay()
    pins = overlay.get("pins") if isinstance(overlay.get("pins"), dict) else {}
    out = dict(DEFAULTS)
    for cls in CLASSES:
        pinned = pins.get(cls)
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


def ref_drift(ref: str, eff: dict[str, str] | None = None) -> str | None:
    """The ref a Claude `anthropic/<id>` reference SHOULD carry, or None when it matches (or is not
    a Claude class ref at all). Used by status, doctor and verify to flag a config that lags the pins."""
    if not isinstance(ref, str) or not ref.startswith("anthropic/"):
        return None
    parsed = parse_id(ref.split("/", 1)[1])
    if not parsed:
        return None
    wanted = openclaw_ref((eff or effective())[parsed[0]])
    return wanted if wanted != ref else None
