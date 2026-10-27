"""A fake OpenClaw host that mirrors the 2026-10-27 production shape, for the orchestration tests (ADR-0019 D1, C1).

Seven orchestrators (main and the channel agents plus the `claude` worker) each allow `claude` and the six by-model
`kit-lane-*` agents; the kit-role agents exist on Anthropic primaries without fallbacks; `agents.defaults.subagents` is
3/3/2. `Host.apply` merges like `openclaw config patch`: objects merge, lists union unless their path is replaced.
"""
from __future__ import annotations

import copy
from pathlib import Path

from ai_resources import repo_root
from ai_resources.setup import effort, state
from ai_resources.setup.cockpits import openclaw_orchestration as O
from ai_resources.setup.cockpits import openclaw_roles as R

LANES = ["kit-lane-agy-gemini38flashhigh", "kit-lane-agy-gemini4argonhigh", "kit-lane-agy-opus55",
         "kit-lane-cli-haiku55", "kit-lane-cli-opus55", "kit-lane-cli-sonnet55"]
CHANNEL_AGENTS = ["main", "snoutzone", "elinvo", "devops", "ai", "security"]
ORCHESTRATORS = [*CHANNEL_AGENTS, "claude"]
CATALOG = {"anthropic": ("claude-sonnet-5-5", "claude-haiku-5-5")}
EFFECTIVE = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5",
             "fable": "claude-fable-5-1"}
OLD_LIMITS = {"maxConcurrent": 3, "maxChildrenPerAgent": 3, "maxSpawnDepth": 2}


def setup_state() -> state.SetupState:
    s = state.SetupState()
    s.profile.name = "balanced"
    return s


def kit_agents():
    agents, _ = R.build_agents(repo_root(), enabled=("anthropic",), catalog=CATALOG,
                               effort_table=effort.table_for_state(setup_state()), ak_path="/kit", by_persona={},
                               env={}, effective=EFFECTIVE)
    return agents


def host_doc(agents=None) -> tuple[dict, state.SetupState]:
    """(config, setup state) of the host before this release: lanes wired, kit-role agents on bare Anthropic primaries."""
    agents = agents if agents is not None else kit_agents()
    entries: dict = {}
    for aid in CHANNEL_AGENTS:
        entries[aid] = {"name": aid, "subagents": {"allowAgents": ["claude", *LANES]}}
    entries["claude"] = {"name": "claude", "subagents": {"allowAgents": list(LANES)}}
    for lane in LANES:
        entries[lane] = {"name": lane, "model": {"primary": "anthropic/claude-sonnet-5-5"},
                         "tools": {"deny": ["bundle-mcp"]}}
    primaries = {}
    for a in agents:
        primaries[a.id] = "anthropic/claude-sonnet-5-5"
        entries[a.id] = {"name": a.id, "model": {"primary": primaries[a.id]},
                         "tools": {"deny": [*R.report_deny_for(a["role"]), "engram__*", "supabase-admin__*"]}}
        if a["thinking"]:
            entries[a.id]["thinkingDefault"] = a["thinking"]
    doc = {"agents": {"defaults": {"model": {"primary": "anthropic/claude-sonnet-5-5"},
                                   "subagents": {"allowAgents": ["claude"], "runTimeoutSeconds": 7200, **OLD_LIMITS}},
                      "entries": entries}}
    s = setup_state()
    s.profile.lanes = {"orchestrators": []}
    s.openclaw.lanes_applied = {
        "created_agent_ids": list(LANES), "created_workspaces": [],
        "allow_agents_added": {o: list(LANES) for o in ORCHESTRATORS}}
    s.openclaw.role_agents_applied = {
        "created_agent_ids": [a.id for a in agents], "models": primaries,
        "thinking": {a.id: a["thinking"] for a in agents}}
    return doc, s


def _union(a: list, b: list) -> list:
    return list(a) + [x for x in b if x not in a]


def _merge(dst: dict, src: dict, path: str, replace: set[str]) -> None:
    for k, v in src.items():
        here = f"{path}.{k}" if path else k
        if v is None:
            dst.pop(k, None)
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v, here, replace)
        elif isinstance(v, list) and isinstance(dst.get(k), list) and here not in replace:
            dst[k] = _union(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)


class Host:
    """Records every patch and every `agents delete`; the config is the live document."""

    def __init__(self, doc: dict):
        self.doc = doc
        self.patches: list[dict] = []
        self.deleted: list[str] = []
        self.registered: list[str] = []

    def apply(self, patch, dry_run=False, replace_paths=None, **_k):
        if not dry_run:
            self.patches.append(copy.deepcopy(patch))
            _merge(self.doc, patch, "", set(replace_paths or []))
        return True, "ok"

    def register(self, aid, ws, model):
        self.registered.append(aid)
        self.doc.setdefault("agents", {}).setdefault("entries", {}).setdefault(aid, {"name": aid})
        return "added"

    def delete(self, aid):
        self.deleted.append(aid)
        self.doc["agents"]["entries"].pop(aid, None)
        return 0, "deleted"

    def writes(self) -> int:
        return len(self.patches) + len(self.deleted) + len(self.registered)


def run_setup(s: state.SetupState, host: Host, tmp_path: Path, *, referenced=lambda _aid: "", dry_run=False,
              infos: list | None = None, agy_ok=lambda _r: True) -> bool:
    """One `setup --auto` pass over the OpenClaw writers that P3 owns: kit-role agents, then the orchestration."""
    say = infos.append if infos is not None else (lambda m: None)
    planned: list[str] = []
    changed = R.configure(
        s, host.doc, config_root=tmp_path / "oc", profile_doc=None, by_persona={}, ak_path="/kit",
        kit_root=repo_root(), enabled=("anthropic",), catalog=CATALOG,
        effort_table=effort.table_for_state(setup_state()), methods={"anthropic": "cli"},
        apply_patch=host.apply, register_agent=host.register, classify=lambda p: [],
        change_records=lambda *a, **k: [], run_changes=[], dry_run=dry_run, info=say, warn=say,
        error=lambda m: (_ for _ in ()).throw(AssertionError(m)), env={}, cli_runtimes=("claude-cli",),
        agy_ok=agy_ok, planned_ids=planned)
    changed |= O.configure(
        s, host.doc, kit_ids=planned, referenced=referenced, apply_patch=host.apply, classify=lambda p: [],
        delete_agent=host.delete, change_records=lambda *a, **k: [], run_changes=[], dry_run=dry_run,
        remove_dir=lambda p: None, info=say, warn=say, error=lambda m: (_ for _ in ()).throw(AssertionError(m)))
    return changed
