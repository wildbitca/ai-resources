"""Antigravity (`agy-cli-lane`) provider plumbing for OpenClaw agents (ADR-0007 s11, ADR-0019).

A kit-role agent whose matrix primary is `agy:<slug>` runs `agy-cli-lane/<slug>`. OpenClaw refuses an unknown model, so the
config needs the catalog stub `models.providers.agy-cli-lane` listing the slugs, the agent denies the tools an agy child
must not hold, and the child gets a kit-owned agy home. This module owns exactly that and nothing
about which agents exist: that is the routing matrix (`openclaw_roles`).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable, Mapping


AGY_BACKEND = "agy-cli-lane"
STUB_PROVIDER = "agy-cli-lane"
STUB_BASE_URL = "http://127.0.0.1:9/"
STUB_API = "openai-completions"          # the schema value is not proven (isolated run pending): see ADR-0007 11
STUB_API_KEY = "kit-lane-agy-placeholder"      # a constant, never a credential
# The OpenClaw tool names an agy child must not hold: the plan's minimum until V7 enumerates them.
AGY_TOOL_DENY = ("message", "sessions_spawn", "cron")
AGY_HOME_PARTS = (".local", "share", "ai-resources", "agy-home")


def agy_home_path() -> Path:
    """The kit-owned agy home the lane uses. Only a path: nothing here creates it, and it is never under /tmp,
    `workspace_root` or `~/.openclaw/kit-lanes/`."""
    return Path.home().joinpath(*AGY_HOME_PARTS)


class LanesWriteError(RuntimeError):
    """The lanes patch could not be built or applied."""


def _get(doc: Any, *keys: str) -> Any:
    cur = doc
    for k in keys:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(k)
    return cur


def agy_slug(agent: Mapping[str, Any]) -> str:
    model = str(agent.get("model") or "")
    return model.split("/", 1)[1] if model.startswith(f"{STUB_PROVIDER}/") else model


def is_agy_ref(ref: Any) -> bool:
    return isinstance(ref, str) and ref.startswith(f"{STUB_PROVIDER}/")


def stub_models(doc: Mapping[str, Any]) -> set[str]:
    return {str(m.get("id")) for m in (_get(doc, "models", "providers", STUB_PROVIDER, "models") or [])
            if isinstance(m, Mapping) and m.get("id")}


def stub_patch_for_slugs(slugs: list[str], doc: Mapping[str, Any]) -> dict[str, Any] | None:
    """The `models.providers.agy-cli-lane` stub for these slugs (fill-only), or None when nothing is missing.

    ADR-0005 finding 1: `sessions_spawn` refuses `Unknown model` without a catalog entry. The key is the placeholder
    constant; an existing provider entry keeps every operator value and only gains missing models."""
    if not slugs:
        return None
    current = _get(doc, "models", "providers", STUB_PROVIDER)
    if not isinstance(current, Mapping):
        return {"baseUrl": STUB_BASE_URL, "api": STUB_API, "apiKey": STUB_API_KEY,
                "models": [{"id": s, "name": s} for s in sorted(set(slugs))]}
    have = [m for m in (current.get("models") or []) if isinstance(m, Mapping)]
    have_ids = {m.get("id") for m in have}
    missing = [s for s in sorted(set(slugs)) if s not in have_ids]
    if not missing:
        return None
    return {"models": have + [{"id": s, "name": s} for s in missing]}


def ensure_agy_home(path: Path) -> dict[str, Any]:
    """Create the kit-owned agy home (0700); never under /tmp or through a symlink."""
    resolved = path.resolve()
    if resolved == Path("/tmp") or Path("/tmp") in resolved.parents:
        raise LanesWriteError(f"the agy home never lives under /tmp: {resolved}")
    if path.is_symlink():
        raise LanesWriteError(f"the agy home is a symlink: {path}")
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return {"path": str(path), "created": not existed}


def remove_agy_home(record: Mapping[str, Any], *, confirm: Callable[[str], bool],
                    remove_dir: Callable[[Path], None] | None = None) -> bool:
    """Remove the agy home only when the kit created it, it is the kit-owned name and the operator confirms."""
    path = Path(str(record.get("path") or ""))
    if not record.get("created") or tuple(path.parts[-len(AGY_HOME_PARTS):]) != AGY_HOME_PARTS:
        return False
    if path.is_symlink() or not path.is_dir():
        return False
    if not confirm(f"Remove the kit-owned agy home {path} (it holds the lane's agy login)?"):
        return False
    (remove_dir or (lambda p: shutil.rmtree(p, ignore_errors=True)))(path)
    return True
