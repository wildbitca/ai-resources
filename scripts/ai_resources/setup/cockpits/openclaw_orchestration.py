"""OpenClaw as the orchestrator of work teams (ADR-0019 D1, C1). Operator-run only.

What it writes, and only this, always through the validated `openclaw config patch` path:

* `agents.entries.<orchestrator>.subagents.allowAgents`: `claude` plus every `kit-role-*` id, merged into what is there
  (an id the operator listed stays; the kit removes only the ids it added). The orchestrators are `main` and every other
  agent that is not a kit agent (the channel agents), plus the ids recorded in the lanes state;
* `agents.defaults.subagents` spawn limits, RAISED to a generous floor and never lowered: the owner runs many agents and
  teams (C1), so setup imposes no cap. What protects the gateway is `bundle-mcp` denied on the `kit-role-*` workers (the
  `claude` worker keeps MCP, it needs it) and a WARNING at `openclaw_reconcile.MCP_CHILD_WARN` MCP children (doctor and the
  reconcile pass report it; nothing kills or reaps a child), sized by measurement (the acceptance run records peak
  memory), not by a hard agent count. Collisions between teams are prevented where the work happens
  (per-run ids and directories, `scope_guard` claims), not by limiting how many run;
* the retirement of the old by-model `kit-lane-*` agents (`migrate_kit_lanes`): only an agent setup created, that nothing
  references, is removed; anything else is kept and reported with the reason;
* the ADOPTION of the by-model lanes an earlier setup wrote and whose record is gone (`adoptable_lanes`, ADR-0022): a
  `kit-lane-*` agent in the old writer's exact shape is managed, never retired, and only filled: the spawn model pin
  (`subagents.model` = its own model), the `claude-cli` pin of its Anthropic refs, the catalog slug of an agy ref and the
  orchestrators' `allowAgents` entry. Nothing an operator set is changed.

Setup never spawns a session and never calls the live gateway. Teardown removes only what `role_agents_applied` records.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .. import ui
from . import openclaw_agy

LANE_PREFIX = "kit-lane-"
ROLE_PREFIX = "kit-role-"
WORKER = "claude"
# C1: a floor, not a ceiling. A value at or above it (the operator's) is never touched.
LIMIT_FLOORS = {"maxSpawnDepth": 4, "maxChildrenPerAgent": 16, "maxConcurrent": 32}
MAX_TEAMS = 16                      # the orchestrator plugin's own schema maximum (openclaw.plugin.json)
OLD_MAX_TEAMS = 2                   # the kit's default before 3.2.0: still the kit's value, so it is raised

Referenced = Callable[[str], str]   # agent id -> why it is in use ("" when nothing references it)


def _get(doc: Any, *keys: str) -> Any:
    cur = doc
    for k in keys:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(k)
    return cur


def _entries(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    e = _get(doc, "agents", "entries")
    return e if isinstance(e, Mapping) else {}


def is_kit_agent(aid: Any) -> bool:
    return isinstance(aid, str) and aid.startswith((LANE_PREFIX, ROLE_PREFIX))


def orchestrator_ids(doc: Mapping[str, Any], recorded: Iterable[str] = (), exclude: Iterable[str] = ()) -> list[str]:
    """`main` and every channel agent in the config: every agent that is not a kit-role or kit-lane agent, plus the ids
    the lanes state names (when they exist), minus `exclude` (`profile.lanes["orchestrators_exclude"]`).

    Every non-kit agent may spawn every kit role, `claude` included, and those have exec and write: an agent the operator
    later restricts on purpose (a public-channel agent with exec denied) must be listed in `exclude`, or it could get
    around its own deny by spawning a worker. Setup does not guess which agents are restricted."""
    entries = _entries(doc)
    ids = {a for a in entries if isinstance(a, str) and not is_kit_agent(a)}
    ids |= {a for a in recorded if isinstance(a, str) and a in entries and not is_kit_agent(a)}
    return sorted(ids - {a for a in exclude if isinstance(a, str)})


def kit_role_ids(doc: Mapping[str, Any], planned: Iterable[str] = ()) -> list[str]:
    """Every `kit-role-*` id the orchestrators may spawn: the ones in the config and the ones about to be written."""
    return sorted({a for a in _entries(doc) if isinstance(a, str) and a.startswith(ROLE_PREFIX)}
                  | {a for a in planned if isinstance(a, str) and a.startswith(ROLE_PREFIX)})


def wiring_patch(doc: Mapping[str, Any], kit_ids: Iterable[str], orchestrators: Iterable[str]) -> dict[str, Any]:
    """Fill-only `allowAgents` for each orchestrator: its current list plus `claude` and the kit-role ids it lacks.

    A `*` entry already allows everything and is left alone; an agent never lists itself."""
    want = [WORKER, *sorted(set(kit_ids))]
    entries = _entries(doc)
    out: dict[str, Any] = {}
    for orch in sorted(set(orchestrators)):
        entry = entries.get(orch)
        if not isinstance(entry, Mapping):
            continue
        current = [a for a in (_get(entry, "subagents", "allowAgents") or []) if isinstance(a, str)]
        if "*" in current:
            continue
        missing = [a for a in want if a != orch and a not in current]
        if missing:
            out[orch] = {"subagents": {"allowAgents": current + missing}}
    return {"agents": {"entries": out}} if out else {}


def limits_patch(doc: Mapping[str, Any]) -> dict[str, Any]:
    """`agents.defaults.subagents`: each limit raised to its floor when it is absent or lower. Never lowered."""
    cur = _get(doc, "agents", "defaults", "subagents")
    cur = cur if isinstance(cur, Mapping) else {}
    raise_ = {k: floor for k, floor in LIMIT_FLOORS.items()
              if not isinstance(cur.get(k), int) or isinstance(cur.get(k), bool) or cur[k] < floor}
    return {"agents": {"defaults": {"subagents": raise_}}} if raise_ else {}


@dataclass
class Plan:
    """What one run would change (the dry-run diff)."""
    allow_added: dict[str, list[str]] = field(default_factory=dict)
    limits: dict[str, tuple[Any, int]] = field(default_factory=dict)       # key -> (old, new)
    lane_removed: list[str] = field(default_factory=list)
    lane_kept: dict[str, str] = field(default_factory=dict)                # id -> reason

    def lines(self) -> list[str]:
        out: list[str] = []
        for orch, ids in sorted(self.allow_added.items()):
            out.append(f"allowAgents {orch}: +{len(ids)} ({', '.join(ids)})")
        for key, (old, new) in sorted(self.limits.items()):
            out.append(f"agents.defaults.subagents.{key}: {'unset' if old is None else old} -> {new}")
        if self.lane_removed:
            out.append("kit-lane agents removed (setup created them, nothing uses them): " + ", ".join(self.lane_removed))
        for aid, why in sorted(self.lane_kept.items()):
            out.append(f"kit-lane agent kept: {aid} ({why})")
        return out


def plan_changes(doc: Mapping[str, Any], kit_ids: Iterable[str], orchestrators: Iterable[str],
                 lanes_recorded: Mapping[str, Any], referenced: Referenced) -> Plan:
    plan = Plan()
    for orch, ent in ((wiring_patch(doc, kit_ids, orchestrators).get("agents") or {}).get("entries") or {}).items():
        live = set(_get(_entries(doc).get(orch), "subagents", "allowAgents") or [])
        plan.allow_added[orch] = [a for a in ent["subagents"]["allowAgents"] if a not in live]
    sub = _get(doc, "agents", "defaults", "subagents") or {}
    for key, new in ((limits_patch(doc).get("agents") or {}).get("defaults", {}).get("subagents") or {}).items():
        plan.limits[key] = (sub.get(key), new)
    plan.lane_removed, plan.lane_kept = plan_kit_lanes(doc, lanes_recorded, referenced)
    return plan


# --- the kit-lane migration ---------------------------------------------------------------------------------------

def _cron_agents(runner: Callable[..., "tuple[int, str]"]) -> "set[str] | None":
    """Agent ids named by a cron job, or None when the job list cannot be read (then nothing is proven unused)."""
    rc, out = runner(["automations", "list", "--all", "--json"])
    start = out.find("{") if rc == 0 else -1
    if start < 0:
        return None
    try:
        doc, _ = json.JSONDecoder().raw_decode(out[start:])
    except ValueError:
        return None
    jobs = doc.get("jobs") if isinstance(doc, Mapping) else None
    if not isinstance(jobs, list):
        return None
    return {j["agentId"] for j in jobs if isinstance(j, Mapping) and isinstance(j.get("agentId"), str)}


def live_referenced(doc: Mapping[str, Any], recorded: Mapping[str, Any], *, state_dir: Path | None,
                    runner: Callable[..., "tuple[int, str]"]) -> Referenced:
    """The `Referenced` test against the live host: a cron job, a channel binding, a session on disk, or an `allowAgents`
    entry setup did not add. A source that cannot be read counts as in use."""
    cron = _cron_agents(runner)
    added = {o: set(ids) for o, ids in (recorded.get("allow_agents_added") or {}).items()}
    bound = {b.get("agentId") for b in (doc.get("bindings") or []) if isinstance(b, Mapping)}

    def check(aid: str) -> str:
        if cron is None:
            return "the cron job list could not be read"
        if aid in cron:
            return "a cron job runs on it"
        if aid in bound:
            return "a channel binding routes to it"
        sessions = (state_dir / aid / "sessions" / "sessions.json") if state_dir is not None else None
        if sessions is not None and sessions.is_file():
            try:
                if json.loads(sessions.read_text(encoding="utf-8")):
                    return "it has sessions"
            except (OSError, ValueError):
                return "its sessions could not be read"
        owners = [o for o, ent in _entries(doc).items() if aid in (_get(ent, "subagents", "allowAgents") or [])
                  and aid not in added.get(o, set())]
        if aid in (_get(doc, "agents", "defaults", "subagents", "allowAgents") or []):
            owners.append("agents.defaults")
        return f"{', '.join(sorted(owners))} lists it in allowAgents (not added by setup)" if owners else ""
    return check


LANE_DIR = "kit-lanes"
CLI_RUNTIME = "claude-cli"


def _primary(model: Any) -> str:
    if isinstance(model, Mapping):
        model = model.get("primary")
    return model if isinstance(model, str) else ""


def _chain(model: Any) -> list[str]:
    fb = model.get("fallbacks") if isinstance(model, Mapping) else None
    return [r for r in [_primary(model), *(fb if isinstance(fb, list) else [])] if isinstance(r, str) and r]


def is_legacy_lane(aid: str, entry: Any) -> bool:
    """A `kit-lane-*` entry in the exact shape the old by-model writer produced: the name and identity are the id, the
    workspace is `<...>/kit-lanes/<id>` and the entry has a primary model. An agent the operator made by hand, or one
    that was edited away from that shape, does not match and stays unmanaged."""
    if not (isinstance(aid, str) and aid.startswith(LANE_PREFIX) and isinstance(entry, Mapping)):
        return False
    ws = entry.get("workspace")
    ident = entry.get("identity")
    return (entry.get("name") == aid and isinstance(ws, str) and Path(ws).name == aid
            and Path(ws).parent.name == LANE_DIR and _primary(entry.get("model")) != ""
            and (not isinstance(ident, Mapping) or ident.get("name") in (None, aid)))


def adoptable_lanes(doc: Mapping[str, Any], recorded: Mapping[str, Any]) -> list[str]:
    """Lanes setup manages without having created them: in the old writer's shape and not in `created_agent_ids` (those
    follow the retirement path)."""
    created = set(recorded.get("created_agent_ids") or [])
    return sorted(a for a, e in _entries(doc).items() if a not in created and is_legacy_lane(a, e))


def adoption_patch(doc: Mapping[str, Any], adopted: Iterable[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(fill-only patch, {id: what it added}) for the adopted lanes.

    * `subagents.model` = the lane's own model when absent: OpenClaw resolves a spawn as `<target>.subagents.model`, then
      `agents.defaults.subagents.model`, then the target's own model (and a requester's model when the spawn inherits
      one), so the pin makes the lane land on its own model whatever the defaults say;
    * the `claude-cli` runtime pin of an Anthropic ref of the chain that lacks it, only when a sibling ref already pins
      it (the lane is a CLI lane); a lane with no pin at all is left alone;
    * the `agy-cli-lane` catalog slug of an agy ref (a spawn is refused `Unknown model` without it)."""
    entries = _entries(doc)
    patch: dict[str, Any] = {}
    filled: dict[str, Any] = {}
    slugs: list[str] = []
    for aid in sorted(set(adopted)):
        ent = entries.get(aid)
        if not isinstance(ent, Mapping):
            continue
        add: dict[str, Any] = {}
        model = ent.get("model")
        if _get(ent, "subagents", "model") is None and model is not None:
            patch.setdefault("agents", {}).setdefault("entries", {}).setdefault(aid, {}).setdefault(
                "subagents", {})["model"] = copy.deepcopy(model)
            add["subagents_model"] = copy.deepcopy(model)
        own = ent.get("models") if isinstance(ent.get("models"), Mapping) else {}
        if any(_get(v, "agentRuntime", "id") == CLI_RUNTIME for v in own.values()):
            for ref in _chain(model):
                if ref.startswith("anthropic/") and not _get(own.get(ref), "agentRuntime", "id"):
                    patch.setdefault("agents", {}).setdefault("entries", {}).setdefault(aid, {}).setdefault(
                        "models", {})[ref] = {"agentRuntime": {"id": CLI_RUNTIME}}
                    add.setdefault("pins", []).append(ref)
        slugs += [r.split("/", 1)[1] for r in _chain(model) if openclaw_agy.is_agy_ref(r)]
        if add:
            filled[aid] = add
    stub = openclaw_agy.stub_patch_for_slugs(slugs, doc)
    if stub:
        patch.setdefault("models", {}).setdefault("providers", {})[openclaw_agy.STUB_PROVIDER] = stub
    return patch, filled


def audit_spawn_models(doc: Mapping[str, Any], ids: Iterable[str]) -> dict[str, tuple[str, str]]:
    """{id: (layer, model)}: the model a `sessions_spawn` to each id resolves to and the config layer it comes from, in
    OpenClaw's order: the target's `subagents.model`, `agents.defaults.subagents.model`, the target's own model."""
    entries = _entries(doc)
    out: dict[str, tuple[str, str]] = {}
    for aid in sorted(set(ids)):
        ent = entries.get(aid) or {}
        if (m := _primary(_get(ent, "subagents", "model"))):
            out[aid] = ("agent subagents.model", m)
        elif (m := _primary(_get(doc, "agents", "defaults", "subagents", "model"))):
            out[aid] = ("agents.defaults.subagents.model", m)
        else:
            out[aid] = ("own model", _primary(ent.get("model")))
    return out


def plan_kit_lanes(doc: Mapping[str, Any], recorded: Mapping[str, Any], referenced: Referenced
                   ) -> tuple[list[str], dict[str, str]]:
    """(removable ids, {kept id: reason}). Removable = created by setup (recorded) AND referenced by nothing.
    An adoptable lane is managed, neither removed nor reported."""
    created = set(recorded.get("created_agent_ids") or [])
    adoptable = set(adoptable_lanes(doc, recorded))
    removable: list[str] = []
    kept: dict[str, str] = {}
    for aid in sorted(a for a in _entries(doc) if isinstance(a, str) and a.startswith(LANE_PREFIX)):
        if aid in adoptable:
            continue
        if aid not in created:
            kept[aid] = "not created by setup"
        elif why := referenced(aid):
            kept[aid] = why
        else:
            removable.append(aid)
    return removable, kept


def _removal_patch(doc: Mapping[str, Any], ids: Iterable[str]) -> dict[str, Any]:
    gone = set(ids)
    out: dict[str, Any] = {}
    for orch, ent in _entries(doc).items():
        allow = _get(ent, "subagents", "allowAgents")
        if isinstance(allow, list) and gone & set(allow):
            out[orch] = {"subagents": {"allowAgents": [a for a in allow if a not in gone]}}
    return {"agents": {"entries": out}} if out else {}


def allow_paths(patch: Mapping[str, Any]) -> list[str]:
    """`--replace-path` for each `allowAgents` list in `patch`: a config patch unions lists, so a removal must replace."""
    ents = ((patch.get("agents") or {}).get("entries") or {})
    return [f"agents.entries.{o}.subagents.allowAgents" for o, e in sorted(ents.items())
            if isinstance(e, Mapping) and "allowAgents" in (e.get("subagents") or {})]


def _contained(ws: str, root: Path | None) -> bool:
    p = Path(ws)
    if root is None or not p.name.startswith(LANE_PREFIX) or p.is_symlink():
        return False
    try:
        rp, rr = p.resolve(), root.resolve()
    except OSError:
        return False
    return rp != rr and rr in rp.parents


def migrate_kit_lanes(s: Any, doc: Mapping[str, Any], *, referenced: Referenced,
                      apply_patch: Callable[..., "tuple[bool, str]"],
                      delete_agent: Callable[[str], "tuple[int, str]"],
                      remove_dir: Callable[[Path], None] | None = None, dry_run: bool = False,
                      info: Callable[[str], None] = print, warn: Callable[[str], None] = print,
                      error: Callable[[str], None] = print) -> tuple[list[str], dict[str, str]]:
    """Retire the by-model lane agents (ADR-0019 O1/O2). Returns (removed, kept: reason).

    Only an id that starts with `kit-lane-`, that the lanes record says setup created and that `referenced` says nothing
    uses is removed: first out of every `allowAgents` list (one validated patch), then `agents delete`. Everything else
    stays and is reported. The record keeps the kept ids so a later run can retire them once nothing references them."""
    o = s.openclaw
    recorded = copy.deepcopy(o.lanes_applied) if o.lanes_applied else {}
    removable, kept = plan_kit_lanes(doc, recorded, referenced)
    for aid, why in sorted(kept.items()):
        info(f"kit-lane agent {aid}: kept: {why}")
    if dry_run or not removable:
        return (removable if dry_run else []), kept
    patch = _removal_patch(doc, removable)
    if patch:
        ok, out = apply_patch(patch, replace_paths=allow_paths(patch))
        if not ok:
            error(f"OpenClaw rejected the allowAgents cleanup: {ui.safe_excerpt(out, 300, tail=True)}")
            return [], {**kept, **{a: "the allowAgents cleanup was rejected" for a in removable}}
    removed: list[str] = []
    rm = remove_dir or (lambda p: shutil.rmtree(p, ignore_errors=True))
    root = Path(str(recorded["workspace_root"])) if recorded.get("workspace_root") else None
    workspaces = list(recorded.get("created_workspaces") or [])
    for aid in removable:
        rc, out = delete_agent(aid)
        if rc != 0:
            error(f"could not delete {aid}: {ui.safe_excerpt(out, 200, tail=True)}")
            kept[aid] = "agents delete failed"
            continue
        removed.append(aid)
        for ws in [w for w in workspaces if Path(w).name == aid]:
            if _contained(ws, root):
                rm(Path(ws))
            workspaces.remove(ws)
    gone = set(removed)
    left = {"created_agent_ids": [a for a in recorded.get("created_agent_ids") or [] if a not in gone],
            "created_workspaces": workspaces}
    if root is not None:
        left["workspace_root"] = str(root)
    added = {o_: [a for a in ids if a not in gone] for o_, ids in (recorded.get("allow_agents_added") or {}).items()}
    left["allow_agents_added"] = {k: v for k, v in added.items() if v}
    o.lanes_applied = {} if not left["created_agent_ids"] else {k: v for k, v in left.items() if v}
    if recorded.get("adopted_agent_ids"):
        o.lanes_applied = {**o.lanes_applied, "adopted_agent_ids": list(recorded["adopted_agent_ids"])}
    if removed:
        info("kit-lane agents removed: " + ", ".join(removed))
    return removed, kept


# --- the wiring ---------------------------------------------------------------------------------------------------

def configure(s: Any, doc: Mapping[str, Any], *, kit_ids: Iterable[str], referenced: Referenced,
              apply_patch: Callable[..., "tuple[bool, str]"], classify: Callable[[dict], list[str]],
              delete_agent: Callable[[str], "tuple[int, str]"],
              change_records: Callable[[dict, dict, Any, str], list[dict]],
              run_changes: list[dict] | None = None, dry_run: bool = False,
              remove_dir: Callable[[Path], None] | None = None,
              info: Callable[[str], None] = print, warn: Callable[[str], None] = print,
              error: Callable[[str], None] = print) -> bool:
    """Wire the orchestrators, raise the limits, retire unused kit-lane agents. True when the config changed.

    Idempotent: a second run finds nothing to add. A patch that needs a gateway restart is deferred, never applied."""
    o = s.openclaw
    lanes = s.profile.lanes if isinstance(getattr(s.profile, "lanes", None), dict) else {}
    orchestrators = orchestrator_ids(doc, lanes.get("orchestrators") or [], lanes.get("orchestrators_exclude") or [])
    adopted = adoptable_lanes(doc, o.lanes_applied or {})
    ids = sorted(set(kit_role_ids(doc, kit_ids)) | set(adopted))
    plan = plan_changes(doc, ids, orchestrators, o.lanes_applied or {}, referenced)
    if dry_run:
        if adopted:
            info("Orchestration (dry run): kit-lane agents adopted: " + ", ".join(adopted))
        for line in plan.lines():
            info("Orchestration (dry run): " + line)
        return False
    recorded = copy.deepcopy(o.role_agents_applied) if o.role_agents_applied else {}
    changed = False
    patch = wiring_patch(doc, ids, orchestrators)
    lim = limits_patch(doc)
    if lim:
        patch = _merge(patch, lim)
    adopt_patch, filled = adoption_patch(doc, adopted)
    if adopt_patch:
        patch = _merge(patch, adopt_patch)
    if patch:
        ok, out = apply_patch(patch, dry_run=True)
        if not ok:
            error(f"OpenClaw rejected the orchestration patch: {ui.safe_excerpt(out, 300, tail=True)}")
            return False
        restart = classify(patch)
        if restart:
            warn("Orchestration keys need a gateway restart and were NOT written (the kit never restarts a live "
                 "gateway): " + ", ".join(sorted(restart)))
            return False
        ok, out = apply_patch(patch)
        if not ok:
            error(f"OpenClaw rejected the orchestration patch: {ui.safe_excerpt(out, 300, tail=True)}")
            return False
        changed = True
        if run_changes is not None:
            run_changes.extend(change_records(dict(doc), patch, None, "orchestration"))
        added = dict(recorded.get("allow_agents_added") or {})
        absent = set(recorded.get("allow_agents_absent") or [])
        for orch, ids_ in plan.allow_added.items():
            if _get(_entries(doc).get(orch), "subagents", "allowAgents") is None:
                absent.add(orch)
            added[orch] = sorted(set(added.get(orch, [])) | set(ids_))
        if filled:
            recorded["adopted_lanes"] = _merge(recorded.get("adopted_lanes") or {}, filled)
        prev = dict(recorded.get("limits_prev") or {})
        for key, (old, _new) in plan.limits.items():
            prev.setdefault(key, old)
        recorded["allow_agents_added"], recorded["allow_agents_absent"] = added, sorted(absent)
        recorded["limits_prev"], recorded["limits_set"] = prev, {**(recorded.get("limits_set") or {}),
                                                                 **{k: v[1] for k, v in plan.limits.items()}}
        o.role_agents_applied = {k: v for k, v in recorded.items() if v not in ({}, [], None)}
        info("Orchestration applied: " + "; ".join(plan.lines()))
    # the migration works on the config after the wiring above (the ids are still listed where setup added them)
    after = _merge(doc, patch) if patch else doc
    if adopted:
        o.lanes_applied = {**(o.lanes_applied or {}),
                           "adopted_agent_ids": sorted(set((o.lanes_applied or {}).get("adopted_agent_ids") or []) | set(adopted))}
        agy_lanes = [a for a in adopted if any(openclaw_agy.is_agy_ref(r) for r in _chain(_get(_entries(after), a, "model")))]
        agy_bin = _get(after, "plugins", "entries", "ai-resources", "config", "agyBin")
        if agy_lanes and not (isinstance(agy_bin, str) and agy_bin.startswith("/")):
            warn("kit-lane agy agents (" + ", ".join(agy_lanes) + ") would reach the catalog stub "
                 f"{openclaw_agy.STUB_BASE_URL} and be refused: plugins.entries.ai-resources.config.agyBin is not an "
                 "absolute path, so the agy-cli-lane backend is not registered")
        for aid, (layer, model) in audit_spawn_models(after, adopted).items():
            own = _primary(_get(_entries(after).get(aid), "model"))
            if layer != "own model" and model != own:
                warn(f"kit-lane agent {aid}: a spawn runs on {model}, not on its own model {own}: {layer} overrides it "
                     f"(operator-owned, left as it is)")
    removed, _kept = migrate_kit_lanes(s, after, referenced=referenced, apply_patch=apply_patch,
                                       delete_agent=delete_agent, remove_dir=remove_dir, info=info, warn=warn,
                                       error=error)
    return changed or bool(removed)


def _merge(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict:
    out = copy.deepcopy(dict(a))
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def restore_fragment(applied: Mapping[str, Any], doc: Mapping[str, Any]) -> dict[str, Any]:
    """The patch that undoes the wiring: `allowAgents` by set difference (an id the operator added later stays) and the
    limits the kit raised, only where the live value is still the one it set."""
    patch: dict[str, Any] = {}
    absent = set(applied.get("allow_agents_absent") or [])
    for orch, ids in sorted((applied.get("allow_agents_added") or {}).items()):
        entry = _entries(doc).get(orch)
        if not isinstance(entry, Mapping):
            continue
        live = list(_get(entry, "subagents", "allowAgents") or [])
        kept = [a for a in live if a not in set(ids)]
        if kept != live:
            patch.setdefault("agents", {}).setdefault("entries", {}).setdefault(orch, {}) \
                .setdefault("subagents", {})["allowAgents"] = kept if (kept or orch not in absent) else None
    for aid, rec in sorted((applied.get("adopted_lanes") or {}).items()):
        live = _entries(doc).get(aid)
        if not isinstance(live, Mapping):
            continue
        ent = patch.setdefault("agents", {}).setdefault("entries", {}).setdefault(aid, {})
        if rec.get("subagents_model") is not None and _get(live, "subagents", "model") == rec["subagents_model"]:
            ent.setdefault("subagents", {})["model"] = None
        for ref in rec.get("pins") or []:
            if _get(_get(live, "models", ref), "agentRuntime", "id") == CLI_RUNTIME:
                ent.setdefault("models", {})[ref] = None
        if not ent:
            patch["agents"]["entries"].pop(aid)
    if patch.get("agents", {}).get("entries") == {}:
        patch.pop("agents")
    sub = _get(doc, "agents", "defaults", "subagents") or {}
    for key, was in (applied.get("limits_prev") or {}).items():
        if sub.get(key) == (applied.get("limits_set") or {}).get(key):
            patch.setdefault("agents", {}).setdefault("defaults", {}).setdefault("subagents", {})[key] = was
    return patch


_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def safe_server_name(raw: str) -> str:
    """OpenClaw's model-facing server name (`sanitizeServerName`): unsafe chars -> `-`, a letter first, <= 30."""
    base = _SAFE.sub("-", raw.strip()) or "mcp"
    if not re.match(r"[A-Za-z]", base):
        base = f"mcp-{base}"
    return base[:30]
