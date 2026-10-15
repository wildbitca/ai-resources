"""OpenClaw writer for subscription lane agents (ADR-0007, plan 4.5). Operator-run only.

What it writes, and only this:

* `agents.entries.<kit-lane-*>`, through OpenClaw's own `agents add` (`openclaw_host.agent_new`, the second
  validated writer), after the patch is classified with `openclaw_reload_rules`;
* `agents.defaults.models.<ref>` catalog entries that are absent (fill-only);
* the lane ids in `agents.entries.<orchestrator>.subagents.allowAgents`, on the named orchestrators only
  (`agents.defaults` is never touched).

Setup never spawns a session and never calls the live gateway: `verify_capability` is derived from the
static config. Teardown removes only what `OpenClawState.lanes_applied` records.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .. import state

PREFIX = "kit-lane-"
CLAUDE_CLI = "claude-cli"
KIT_LANES_DIR = "kit-lanes"
_EXEC_OFF = {"", "off", "deny", "none", "disabled"}


class LanesWriteError(RuntimeError):
    """The lanes patch could not be built or applied."""


def _get(doc: Any, *keys: str) -> Any:
    cur = doc
    for k in keys:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(k)
    return cur


def kit_agent_ids(lanes: Mapping[str, Any]) -> list[str]:
    """Only kit ids are ever written or removed: an agent not named `kit-lane-*` is never ours."""
    return sorted(a for a in (lanes.get("agents") or {}) if isinstance(a, str) and a.startswith(PREFIX))


def workspace_root(lanes: Mapping[str, Any], config_root: Path) -> Path:
    raw = lanes.get("workspace_root") or ""
    root = Path(raw).expanduser() if raw else config_root / KIT_LANES_DIR
    resolved = root.resolve()
    if resolved == Path("/tmp") or Path("/tmp") in resolved.parents:
        raise LanesWriteError(f"lane workspaces never live under /tmp (tmpfs, emptied on reboot): {resolved}")
    return root


def verify_capability(doc: Mapping[str, Any], orchestrators: Iterable[str]) -> dict[str, dict[str, str]]:
    """Static record per orchestrator (plan R7-6): from its tool grants in the config, never a spawn.

    exec granted -> `exec`; the kit plugin tool granted -> `plugin-tool`; a claude-cli orchestrator without an
    explicit grant -> `exec` by reference to the isolated V8 evidence; anything else -> `none`.
    """
    out: dict[str, dict[str, str]] = {}
    entries = _get(doc, "agents", "entries") or {}
    for orch in sorted(set(orchestrators)):
        entry = entries.get(orch) if isinstance(entries, Mapping) else None
        entry = entry if isinstance(entry, Mapping) else {}
        mode = _get(entry, "tools", "exec", "mode")
        allow = list(_get(entry, "tools", "allow") or []) + list(_get(entry, "tools", "alsoAllow") or [])
        if any(isinstance(a, str) and "lanes" in a for a in allow):
            out[orch] = {"mode": "plugin-tool", "source": "static-config", "evidence_ref": ""}
        elif isinstance(mode, str) and mode.lower() not in _EXEC_OFF:
            out[orch] = {"mode": "exec", "source": "static-config", "evidence_ref": ""}
        elif str(_get(entry, "model", "primary") or "").startswith(("claude-cli/", "claude-kit/")):
            out[orch] = {"mode": "exec", "source": "isolated-evidence",
                         "evidence_ref": "docs/decisions/0007-subscription-lanes.md#evidence-v8"}
        else:
            out[orch] = {"mode": "none", "source": "static-config", "evidence_ref": ""}
    return out


def _agent_entry(agent_id: str, agent: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    return {"name": agent_id, "workspace": str(workspace), "model": {"primary": agent.get("model", "")}}


def _catalog_entry(agent: Mapping[str, Any]) -> dict[str, Any] | None:
    if agent.get("backend") == CLAUDE_CLI:
        return {"agentRuntime": {"id": CLAUDE_CLI}}
    return None      # an agy catalog stub is phase 3 and not written here


def build_patch(lanes: Mapping[str, Any], doc: Mapping[str, Any], workspaces: Mapping[str, Path], *,
                ids: Iterable[str] | None = None) -> dict[str, Any]:
    """The fill-only lanes patch for `doc`. Empty when nothing is missing.

    Entries are only added for ids absent from the config; the catalog only for refs absent from
    `agents.defaults.models`; `allowAgents` receives the ids missing from each NAMED orchestrator that
    exists in the config. `agents.defaults.subagents` and every other agent are never touched.
    """
    agents = lanes.get("agents") or {}
    wanted = [i for i in kit_agent_ids(lanes) if ids is None or i in set(ids)]
    entries = _get(doc, "agents", "entries") or {}
    catalog = _get(doc, "agents", "defaults", "models") or {}
    patch: dict[str, Any] = {}
    for aid in wanted:
        if aid not in entries:
            patch.setdefault("agents", {}).setdefault("entries", {})[aid] = _agent_entry(
                aid, agents[aid], workspaces[aid])
        cat = _catalog_entry(agents[aid])
        ref = agents[aid].get("model", "")
        if cat is not None and ref and ref not in catalog:
            patch.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {})[ref] = cat
    for orch in sorted(set(lanes.get("orchestrators") or [])):
        entry = entries.get(orch) if isinstance(entries, Mapping) else None
        if not isinstance(entry, Mapping):
            continue
        current = list(_get(entry, "subagents", "allowAgents") or [])
        missing = [i for i in wanted if i not in current]
        if missing:
            patch.setdefault("agents", {}).setdefault("entries", {}).setdefault(orch, {}) \
                .setdefault("subagents", {})["allowAgents"] = current + missing
    return patch


def restore_fragment(applied: Mapping[str, Any], doc: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The patch that removes exactly what the kit added, from what is live now.

    `allowAgents` by set difference (an id the operator added later stays); catalog refs the kit added are
    deleted; agent entries are not here: `agents delete` removes them (and only recorded ids).
    """
    patch: dict[str, Any] = {}
    entries = _get(doc, "agents", "entries") or {}
    absent = set(applied.get("allow_agents_absent") or [])
    for orch, ids in sorted((applied.get("allow_agents_added") or {}).items()):
        entry = entries.get(orch) if isinstance(entries, Mapping) else None
        if not isinstance(entry, Mapping):
            continue
        live = list(_get(entry, "subagents", "allowAgents") or [])
        kept = [x for x in live if x not in set(ids)]
        if kept == live:
            continue
        value: Any = kept if (kept or orch not in absent) else None
        patch.setdefault("agents", {}).setdefault("entries", {}).setdefault(orch, {}) \
            .setdefault("subagents", {})["allowAgents"] = value
    catalog = _get(doc, "agents", "defaults", "models") or {}
    for ref in applied.get("catalog_added") or []:
        if ref in catalog:
            patch.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {})[ref] = None
    return patch, []


def configure(s: "state.SetupState", doc: Mapping[str, Any], *, config_root: Path,
              apply_patch: Callable[..., "tuple[bool, str]"],
              register_agent: Callable[[str, Path, str], str],
              classify: Callable[[dict], list[str]],
              change_records: Callable[[dict, dict, Any, str], list[dict]],
              run_changes: list[dict] | None = None, dry_run: bool = False,
              info: Callable[[str], None] = print, warn: Callable[[str], None] = print,
              error: Callable[[str], None] = print,
              confirm: Callable[[str], bool] = lambda _m: True,
              teardown_fn: Callable[..., bool] | None = None, **teardown_kw: Any) -> bool:
    """Apply (or, with no lanes in the state, tear down) the lanes writes. True when the config changed.

    `register_agent(id, workspace, model) -> "added" | "existing" | "failed: ..."` is `openclaw_host.agent_new`.
    Nothing is written when `dry_run`; a patch that needs a gateway restart is deferred, never applied.
    """
    o = s.openclaw
    lanes = s.profile.lanes if state.lanes_enabled(s) else None
    if lanes is None:
        if o.lanes_applied and teardown_fn is not None:
            return teardown_fn(s, doc, **teardown_kw)
        return False
    ids = kit_agent_ids(lanes)
    if not ids:
        return False
    try:
        root = workspace_root(lanes, config_root)
    except LanesWriteError as e:
        error(str(e))
        return False
    workspaces = {i: root / i for i in ids}
    patch = build_patch(lanes, doc, workspaces)
    lanes["verify_capability"] = verify_capability(doc, lanes.get("orchestrators") or [])
    if dry_run:
        info("Subscription lanes (dry run): " + (", ".join(sorted(_leaf_paths(patch))) or "no change"))
        return False
    entries = _get(doc, "agents", "entries") or {}
    new_ids = [i for i in ids if i not in entries]
    if patch:
        ok, out = apply_patch(patch, dry_run=True)
        if not ok:
            error(f"OpenClaw rejected the lanes patch: {out[-300:]}")
            return False
        restart = classify(patch)
        if restart:
            o.lanes_applied = {**o.lanes_applied, "deferred": sorted(restart)}
            warn("Lane keys need a gateway restart and were NOT written (the kit never restarts a live "
                 "gateway): " + ", ".join(sorted(restart)))
            return False
    applied = copy.deepcopy(o.lanes_applied) if o.lanes_applied else {}
    applied.pop("deferred", None)
    created = list(applied.get("created_agent_ids") or [])
    created_ws = list(applied.get("created_workspaces") or [])
    ready = [i for i in ids if i in entries]
    for aid in new_ids:
        ws = workspaces[aid]
        existed = ws.exists()
        ws.mkdir(parents=True, exist_ok=True)
        status = register_agent(aid, ws, str(lanes["agents"][aid].get("model", "")))
        if status == "added":
            created.append(aid)
            if not existed:
                created_ws.append(str(ws))
            ready.append(aid)
        elif status == "existing":
            ready.append(aid)
        else:
            error(f"could not add lane agent {aid}: {status}")
            if not existed:
                try:
                    ws.rmdir()
                except OSError:
                    pass
    # Re-read nothing: the second patch is built from the same snapshot, limited to the agents that exist now.
    doc2 = copy.deepcopy(dict(doc))
    ents = doc2.setdefault("agents", {}).setdefault("entries", {})
    for aid in ready:
        ents.setdefault(aid, {})
    patch2 = build_patch(lanes, doc2, workspaces, ids=ready)
    changed = bool(new_ids and created)
    if patch2:
        ok, out = apply_patch(patch2)
        if not ok:
            error(f"OpenClaw rejected the lanes patch: {out[-300:]}")
        else:
            changed = True
            records = change_records(dict(doc2), patch2, None, "lanes")
            if run_changes is not None:
                run_changes.extend(records)
            added = dict(applied.get("allow_agents_added") or {})
            absent = set(applied.get("allow_agents_absent") or [])
            for orch, ent in ((patch2.get("agents") or {}).get("entries") or {}).items():
                if orch in ready or "subagents" not in ent:
                    continue
                before = list(_get(entries.get(orch) or {}, "subagents", "allowAgents") or [])
                if _get(entries.get(orch) or {}, "subagents", "allowAgents") is None:
                    absent.add(orch)
                new = [i for i in ent["subagents"]["allowAgents"] if i not in before]
                added[orch] = sorted(set(added.get(orch, [])) | set(new))
            applied["allow_agents_added"] = added
            applied["allow_agents_absent"] = sorted(absent)
            cat = sorted(set(applied.get("catalog_added") or [])
                         | set(((patch2.get("agents") or {}).get("defaults") or {}).get("models") or {}))
            applied["catalog_added"] = cat
    applied["created_agent_ids"] = sorted(set(created))
    applied["created_workspaces"] = sorted(set(created_ws))
    o.lanes_applied = {k: v for k, v in applied.items() if v not in ({}, [], None)}
    if changed:
        info("Subscription lanes applied: " + ", ".join(ready))
    return changed


def _leaf_paths(patch: Mapping[str, Any], prefix: str = "") -> list[str]:
    out: list[str] = []
    for k, v in patch.items():
        path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, Mapping) and v:
            out.extend(_leaf_paths(v, path))
        else:
            out.append(path)
    return out


def teardown(s: "state.SetupState", doc: Mapping[str, Any], *,
             apply_patch: Callable[..., "tuple[bool, str]"],
             oc: Callable[..., "tuple[int, str]"],
             confirm: Callable[[str], bool] = lambda _m: True,
             remove_dir: Callable[[Path], None] | None = None,
             info: Callable[[str], None] = print, error: Callable[[str], None] = print) -> bool:
    """Remove only what the kit recorded: `allowAgents` ids (set difference), catalog refs it added,
    agents it created (`openclaw agents delete <id> --force`, after confirmation) and its workspaces."""
    o = s.openclaw
    applied = o.lanes_applied or {}
    if not applied:
        return False
    frag, _rp = restore_fragment(applied, doc)
    if frag:
        ok, out = apply_patch(frag)
        if not ok:
            error(f"OpenClaw lanes teardown failed: {out[-300:]}")
            return False
    remaining_ids: list[str] = []
    for aid in applied.get("created_agent_ids") or []:
        if not (isinstance(aid, str) and aid.startswith(PREFIX)):
            continue                                  # never delete an id that is not a kit lane id
        if not confirm(f"Delete the lane agent {aid} that setup created?"):
            remaining_ids.append(aid)
            continue
        rc, out = oc(["agents", "delete", aid, "--force"])
        if rc != 0:
            error(f"could not delete lane agent {aid}: {out[-200:]}")
            remaining_ids.append(aid)
    import shutil
    rm = remove_dir or (lambda p: shutil.rmtree(p, ignore_errors=True))
    kept_ws: list[str] = []
    for ws in applied.get("created_workspaces") or []:
        if Path(ws).name in remaining_ids:
            kept_ws.append(ws)
            continue
        rm(Path(ws))
    if remaining_ids:
        o.lanes_applied = {"created_agent_ids": remaining_ids, "created_workspaces": kept_ws}
        return True
    o.lanes_applied = {}
    info("Subscription lanes removed (only kit lane ids).")
    return True
