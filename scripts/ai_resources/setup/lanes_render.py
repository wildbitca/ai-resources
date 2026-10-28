"""Subscription lanes: the setup-owned state fragment and the `lanes.yaml` rendering (ADR-0007, ADR-0019).

`~/.config/ai-resources/lanes.yaml` is generated purely from `scoring.routing.ROLES` (plus the profile's `routing:`
overrides): the role matrix, the spill rule, and a digest. It carries no agents, candidates
or resolved table. It is written only when lanes are enabled, in both modes, and does not depend on model ids (a model
move re-renders nothing). Nothing here opens `openclaw.json`.

Pure helpers first; the one file writer (`sync_file`) is the only function with side effects, and it backs the file
up before writing it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

from ..scoring import routing as scoring_routing
from . import state

try:
    import yaml
except ImportError:  # pragma: no cover - handled by ui.require_deps()
    yaml = None  # type: ignore

GEMINI_FLASH_REF = "google/gemini-3.8-flash"      # the utility model of the balanced credit policy (ADR-0017)


class LanesBlockError(ValueError):
    """The profile's `lanes:` block is malformed."""


def render_doc(lanes: Mapping[str, Any], mode: str) -> dict[str, Any]:
    """The `lanes.yaml` document: `{mode, generated_from, routing, digest}`, a pure function of the matrix.

    `routing` is `scoring.routing.render_section` over the profile's `routing:` overrides (kept in the lanes state)."""
    payload: dict[str, Any] = {"mode": mode, "generated_from": "scoring.routing"}
    payload["routing"] = scoring_routing.render_section({"routing": lanes.get("routing")} if lanes.get("routing") else None)
    return {**payload, "digest": doc_digest(payload)}


def doc_digest(payload: Mapping[str, Any]) -> str:
    """The digest of a `lanes.yaml` document: every key except `digest` itself."""
    body = {k: v for k, v in payload.items() if k != "digest"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def dumps(doc: Mapping[str, Any]) -> str:
    if yaml is None:
        raise RuntimeError("PyYAML required to write lanes.yaml")
    return ("# Generated from scoring.routing by `ai-resources setup` (ADR-0019). Do not edit: setup re-renders it.\n"
            + yaml.safe_dump(dict(doc), default_flow_style=False, sort_keys=False))


# --- the writer (backup before write, restore from its own backup) ---------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    from ..openclaw_host import write_file_atomically       # unique temp file: two setups never tear lanes.yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    write_file_atomically(path, text, mode=0o600)


MULTI_PROVIDER = "multi-provider"


def team_mode(s: "state.SetupState") -> str:
    """The label `lanes.yaml` carries for `mode`.

    `state.mode` only says whether a LiteLLM/OpenRouter gateway fronts the models: `single-model` means "no gateway", not
    "one model". A host whose team runs through the CLIs and APIs (`cli_team`, the default since ADR-0015) spreads the
    roles over Claude Code, Antigravity, the Anthropic API and the Gemini API, so its file says `multi-provider`. A host
    that really runs a gateway keeps `multi-model`; a host with neither keeps `single-model`."""
    if s.mode == "multi-model":
        return "multi-model"
    return MULTI_PROVIDER if (getattr(s, "cli_team", False) or getattr(s, "cli_team_api", False)) else s.mode


def expected_text(s: "state.SetupState") -> str | None:
    """The `lanes.yaml` text for this state, or None when there are no lanes."""
    if not state.lanes_enabled(s):
        return None
    return dumps(render_doc(s.profile.lanes, team_mode(s)))


def backup(path: Path, backup_dir: Path | None) -> dict[str, Any]:
    """Copy `path` into `backup_dir` (0600) and say what existed, so a restore never deletes a file the
    writer did not create."""
    payload: dict[str, Any] = {"path": str(path), "existed": path.is_file(), "backup": None}
    if payload["existed"]:
        payload["mode"] = stat.S_IMODE(path.stat().st_mode)
        if backup_dir is not None:
            backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            dest = backup_dir / f"lanes-{path.name}"
            dest.write_bytes(path.read_bytes())
            os.chmod(dest, 0o600)
            payload["backup"] = str(dest)
    return payload


def restore(payload: Mapping[str, Any]) -> None:
    path = Path(payload["path"])
    if payload.get("backup"):
        src = Path(payload["backup"])
        if not src.is_file():
            raise OSError(f"the backup {src} is gone; cannot restore {path}")
        path.write_bytes(src.read_bytes())
        if payload.get("mode") is not None:
            os.chmod(path, payload["mode"])
    elif payload.get("existed"):
        raise OSError(f"no backup of {path} was taken; it was left as it is")
    else:
        path.unlink(missing_ok=True)


def sync_file(s: "state.SetupState", *, backup_dir: Path | None = None) -> dict[str, Any]:
    """Make `lanes.yaml` match the state, tracking it in `tracking.config_files_written`.

    Lanes non-empty -> render (backed up first); lanes empty -> remove the file and drop the tracking
    entry. Returns `{"action": written|removed|unchanged, "payload": <restore payload or None>}`.
    Never reads or writes `openclaw.json`.
    """
    path = state.lanes_path()
    text = expected_text(s)
    tracked = s.tracking.config_files_written
    if text is None:
        payload = None
        action = "unchanged"
        if path.is_file() and str(path) in tracked:
            payload = backup(path, backup_dir)
            path.unlink()
            action = "removed"
        if str(path) in tracked:
            tracked.remove(str(path))
        return {"action": action, "payload": payload}
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        if str(path) not in tracked:
            tracked.append(str(path))
        return {"action": "unchanged", "payload": None}
    payload = backup(path, backup_dir)
    try:
        _atomic_write(path, text)
    except Exception:
        restore(payload)
        raise
    if str(path) not in tracked:
        tracked.append(str(path))
    return {"action": "written", "payload": payload}


# --- enabling lanes (the wizard's lane step) -------------------------------------------------------------

def lanes_state(profile_doc: Mapping[str, Any], *, orchestrators: Any = ()) -> dict[str, Any]:
    """The `ProfileState.lanes` fragment a profile proposes: orchestrators and the profile's `routing:` overrides. The role table itself is never stored: it is `scoring.routing.ROLES` (+ overrides) every time.

    Raises `LanesBlockError` for a malformed `routing:` block. Nothing is written."""
    out: dict[str, Any] = {"orchestrators": sorted({str(o) for o in orchestrators})}
    if profile_doc.get("routing"):
        try:
            scoring_routing.merged(profile_doc)      # fail early on a malformed override
        except scoring_routing.RoutingError as exc:
            raise LanesBlockError(str(exc)) from exc
        out["routing"] = profile_doc["routing"]
    return out


def routing_doc(s: "state.SetupState") -> dict[str, Any]:
    """`{"routing": overrides}` for this state, or `{}`: the lanes record's own overrides when lanes are enabled, else the
    saved profile's. The single reader every generator of the matrix (lanes.yaml, kit-role agents, setup tables) uses."""
    lanes = s.profile.lanes if isinstance(s.profile.lanes, dict) else {}
    if lanes.get("routing"):
        return {"routing": lanes["routing"]}
    try:
        from . import profiles
        mode = "multi-model" if s.mode == "multi-model" else "single-model"
        doc = profiles.load_profile(profiles.saved_or_recommended(s.profile.name, s.backend), mode)
    except Exception:  # noqa: BLE001 - a missing profile means the default matrix
        return {}
    return {"routing": doc["routing"]} if doc.get("routing") else {}


def lane_roles(profile_doc: Mapping[str, Any] | None = None) -> list[str]:
    """The team roles of the matrix, sorted."""
    return sorted(r.name for r in scoring_routing.merged(profile_doc).values() if r.kind == "team")
