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
