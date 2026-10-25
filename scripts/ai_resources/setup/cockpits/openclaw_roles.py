"""One OpenClaw agent per kit role (ADR-0015 S8, item a).

`kit-role-<role>` agents (and `kit-role-<role>-<domain>` for the active persona domains) are what the native team
orchestrator dispatches to. Each carries:

* the role prompt (`role_render.RoleSpec.prompt`) as the AGENTS.md of its own workspace under `<config>/kit-roles/<id>/`,
* the model the profile gives the role, restricted to the providers the operator enabled (`_openclaw_providers.route_roles`),
* `thinkingDefault` from the effort table (`setup/effort.py`),
* a `skills` allowlist (the role's `skill_scope.ROLE_SKILLS` plus `kit-orchestration`).

Written like the lane agents (ADR-0007 s10): `openclaw agents add` through `openclaw_host.agent_new`, then ONE fill-only
config patch classified with `openclaw_reload_rules` BEFORE anything is written (a restart-class key is deferred, never
applied: ADR-0003). Only recorded ids are ever updated or removed; an entry whose model the operator changed is kept and
listed. Setup never spawns a session and never calls the live gateway.
"""
from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .. import effort as effort_mod
from .. import role_render, state, ui
from ..live_openclaw import _primary
from ... import model_pins, roles_policy, skill_scope
from . import _openclaw_providers as providers_mod
from . import _shared

PREFIX = "kit-role-"
ROOT_DIR = "kit-roles"
MAX_ID = 31                                  # openclaw_host._AGENT_ID: a letter, then letters, digits and dashes (31 in all)
ALWAYS_SKILLS = ("kit-orchestration",)

# role -> the short word in the agent id (ids are capped at 31 characters, persona suffix included).
SHORT = {
    "software-architect": "architect", "code-reviewer": "reviewer", "security-auditor": "security",
    "doc-writer": "docs", "package-upgrade": "upgrade", "generalPurpose": "general",
    "crashlytics-fixer": "crashlytics", "sentry-fixer": "sentry", "terraform-maintainer": "terraform",
    "crossplane-upjet-maintainer": "crossplane",
}
DOMAIN_SHORT = {"dart-flutter": "flutter", "api-platform": "api"}
# Providers that sign in through a CLI backend run their models through that runtime: a pin in the role agent's OWN
# `models` map (fill-only), never in `agents.defaults.models`, so the operator's agents keep the runtime they have.
RUNTIME_OF_PROVIDER = {"anthropic": "claude-cli", "openai": "codex"}


def agent_id_for(role: str, domain: str = "") -> str:
    """`kit-role-<short role>[-<short domain>]`: lowercase letters, digits and dashes, at most 31 characters."""
    base = SHORT.get(role, re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-"))
    suffix = DOMAIN_SHORT.get(domain, domain)
    out = PREFIX + base + (f"-{suffix}" if suffix else "")
    return out[:MAX_ID].rstrip("-")


def is_kit_role_id(agent_id: Any) -> bool:
    return isinstance(agent_id, str) and agent_id.startswith(PREFIX)


class RoleAgent(dict):
    """One agent to write; a dict so it serialises into the run record unchanged."""

    @property
    def id(self) -> str:
        return self["id"]


def resolve_ref(model: Any, effective: Mapping[str, str] | None = None) -> str:
    """The OpenClaw model ref of a profile role model: an alias (`sonnet`), a bare id or `provider/model`."""
    if isinstance(model, dict):
        model = model.get("model")
    if not isinstance(model, str) or not model:
        return ""
    if "/" in model:
        return model
    eff = effective if effective is not None else model_pins.effective()
    if model in model_pins.CLASSES:
        return model_pins.openclaw_ref(eff.get(model) or model_pins.DEFAULTS[model])
    if model == "inherit":
        return model_pins.openclaw_ref(eff.get("sonnet") or model_pins.DEFAULTS["sonnet"])
    prov = providers_mod.role_provider(model)
    return f"{prov}/{model}" if prov else model


def build_agents(kit_root: Path, by_role: Mapping[str, Any], *, enabled: Iterable[str] | None,
                 catalog: Mapping[str, Iterable[str]] | None, effort_table: Mapping[str, str] | None,
                 ak_path: str, by_persona: Mapping[str, Any] | None = None, env: dict | None = None,
                 effective: Mapping[str, str] | None = None
                 ) -> tuple[list[RoleAgent], list[tuple[str, str, str]]]:
    """(agents, [(role, wanted provider, fallback ref)]) for the 15 roles and the active persona domains. Pure."""
    specs = role_render.load_specs(kit_root, env=env)
    resolved = {r: resolve_ref(spec, effective) for r, spec in by_role.items()}
    routed, fell = providers_mod.route_roles({r: ref for r, ref in resolved.items() if ref}, enabled, catalog)
    persona_refs = {p: resolve_ref(spec, effective) for p, spec in (by_persona or {}).items()}
    out: list[RoleAgent] = []
    for spec in specs:
        ref = persona_refs.get(spec.name) or routed.get(spec.base) or ""
        if spec.is_persona and persona_refs.get(spec.name):
            ref = providers_mod.route_roles({spec.name: ref}, enabled, catalog)[0].get(spec.name, ref)
        if not ref:
            continue                                              # a role the profile does not map is not written
        domain = spec.name[len(spec.base) + 1:] if spec.is_persona else ""
        level = (effort_table or {}).get(spec.base)
        prompt = spec.prompt(lambda b: b.replace("$AGENT_KIT", ak_path), _shared.kit_context_block(ak_path))
        out.append(RoleAgent(
            id=agent_id_for(spec.base, domain), role=spec.base, name=spec.name, domain=domain,
            description=" ".join(spec.description.split()), model=ref,
            thinking=effort_mod.for_cockpit(level, "openclaw") if level else "",
            skills=sorted(set(spec.skills) | set(ALWAYS_SKILLS)),
            prompt=prompt, prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest()))
    return out, fell


def _get(doc: Any, *keys: str) -> Any:
    cur = doc
    for k in keys:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(k)
    return cur


def runtime_for(agent: RoleAgent, methods: Mapping[str, str], cli_runtimes: Iterable[str] = ()) -> str:
    """The CLI runtime a role agent runs its model through, or "" for OpenClaw's own route of that provider.

    A provider OpenClaw signs in through a CLI backend uses it; so does a runtime in `cli_runtimes`, the CLIs the
    operator has signed in (Claude Code: the kit team runs on the operator's CLIs, ADR-0015)."""
    provider = providers_mod.provider_of_ref(agent["model"])
    runtime = RUNTIME_OF_PROVIDER.get(provider) or ""
    if runtime and (methods.get(provider) == "cli" or runtime in set(cli_runtimes)):
        return runtime
    return ""


def _pin(runtime: str) -> dict[str, Any]:
    return {"agentRuntime": {"id": runtime}}


# Report roles read and never edit files. The ones that need no shell either lose exec and process too: the claude-cli
# runtime turns tools.deny into Claude Code's --disallowedTools (bash is exec), so this removes Bash for them. OpenClaw's
# sandbox does not reach a CLI runtime (Claude Code owns its own sandboxing), so tool policy is the lever here.
REPORT_DENY = ("write", "edit", "apply_patch")
NO_EXEC_DENY = ("exec", "process")
NO_EXEC_ROLES = frozenset({"explore", "planner", "software-architect"})
# tools.deny lists an earlier kit wrote: an entry still carrying one of them is moved to today's list.
PREVIOUS_DENIES = (list(REPORT_DENY),)


def deny_for(role: str) -> list[str]:
    if role not in roles_policy.REPORT_ROLES:
        return []
    return list(REPORT_DENY) + (list(NO_EXEC_DENY) if role in NO_EXEC_ROLES else [])


def _tools_patch(live: Mapping[str, Any], agent: RoleAgent) -> "list[str] | None":
    """Today's deny list when the live entry still carries one an earlier kit wrote; None otherwise (the operator's)."""
    want = deny_for(agent.get("role", ""))
    have = _get(live, "tools", "deny")
    if want and isinstance(have, list) and have != want and have in PREVIOUS_DENIES:
        return want
    return None


def _entry(agent: RoleAgent, workspace: Path, runtime: str = "") -> dict[str, Any]:
    entry: dict[str, Any] = {"name": agent.id, "description": agent["description"][:200], "workspace": str(workspace),
                             "model": {"primary": agent["model"]}, "skills": list(agent["skills"])}
    if runtime:
        entry["models"] = {agent["model"]: _pin(runtime)}
    if agent["thinking"]:
        entry["thinkingDefault"] = agent["thinking"]
    deny = deny_for(agent.get("role", ""))
    if deny:
        entry["tools"] = {"deny": deny}
    return entry


_SHELL_KEYS = frozenset({"name", "identity", "workspace", "agentDir", "model"})


def _registration_shell(live: Mapping[str, Any], agent: RoleAgent, workspace: Path) -> bool:
    """True for the bare entry `openclaw agents add` writes for a kit role: only its keys, the kit's workspace and model."""
    identity = live.get("identity", {"name": agent.id})
    return (set(live) <= _SHELL_KEYS and live.get("workspace") == str(workspace)
            and _primary(live.get("model")) == agent["model"] and identity == {"name": agent.id})


def build_patch(agents: list[RoleAgent], doc: Mapping[str, Any], workspaces: Mapping[str, Path],
                recorded: Mapping[str, Any], methods: Mapping[str, str],
                cli_runtimes: Iterable[str] = ()) -> tuple[dict[str, Any], list[str]]:
    """(fill-only patch, ids kept because the operator changed them). Empty patch when nothing is missing.

    A new id gets its whole entry. An existing id is updated only when it is a recorded kit id whose live model and
    thinking still equal what the kit last wrote (so a profile change moves it); otherwise it is the operator's and is
    kept. The bare entry `agents add` leaves when a run stops early is the kit's and gets its fields. A role whose model runs through a CLI (`runtime_for`) gets that runtime in its own `models` map; a model the
    kit moves away from loses the pin the kit gave it."""
    entries = _get(doc, "agents", "entries") or {}
    last = recorded.get("models") or {}
    last_think = recorded.get("thinking") or {}
    patch: dict[str, Any] = {}
    kept: list[str] = []
    for agent in agents:
        aid = agent.id
        live = entries.get(aid) if isinstance(entries, Mapping) else None
        runtime = runtime_for(agent, methods, cli_runtimes)
        if not isinstance(live, Mapping):
            patch.setdefault("agents", {}).setdefault("entries", {})[aid] = _entry(agent, workspaces[aid], runtime)
        else:
            live_model = _primary(live.get("model"))   # OpenClaw also takes the bare-string form
            live_think = live.get("thinkingDefault", "")
            ours = aid in set(recorded.get("created_agent_ids") or [])
            if _registration_shell(live, agent, workspaces[aid]):
                # `agents add` ran but the run stopped before the kit's fields landed: finish the kit's own entry
                # (the model too: `agents add` spells it as a bare string, which `verify` warns about)
                ent = {k: v for k, v in _entry(agent, workspaces[aid], runtime).items() if k != "name"}
                patch.setdefault("agents", {}).setdefault("entries", {})[aid] = ent
                continue
            if live_model == agent["model"] and (live_think or "") == (agent["thinking"] or ""):
                if isinstance(live.get("model"), str):
                    # same model in the short form `agents add` writes: spell it as the object the kit writes
                    patch.setdefault("agents", {}).setdefault("entries", {}).setdefault(aid, {})["model"] = {
                        "primary": agent["model"]}
            elif ours and live_model == last.get(aid) and (live_think or "") == (last_think.get(aid) or ""):
                ent = {"model": {"primary": agent["model"]}}
                if agent["thinking"]:
                    ent["thinkingDefault"] = agent["thinking"]
                elif live_think:
                    ent["thinkingDefault"] = None
                own = _get(live, "models") or {}
                old_ref = last.get(aid)
                if old_ref and old_ref != agent["model"] and _get(own.get(old_ref) or {}, "agentRuntime", "id") \
                        in RUNTIME_OF_PROVIDER.values():
                    ent["models"] = {old_ref: None}                 # the pin of a model this agent no longer runs
                patch.setdefault("agents", {}).setdefault("entries", {})[aid] = ent
            else:
                kept.append(aid)
                continue
        own = _get(live, "models") or {}
        if runtime and not _get(own.get(agent["model"]) or {}, "agentRuntime", "id"):
            ent = patch.setdefault("agents", {}).setdefault("entries", {}).setdefault(aid, {})
            ent.setdefault("models", {})[agent["model"]] = _pin(runtime)
        deny = _tools_patch(live, agent) if isinstance(live, Mapping) else None
        if deny is not None:
            patch.setdefault("agents", {}).setdefault("entries", {}).setdefault(aid, {})["tools"] = {"deny": deny}
    return patch, kept


def workspace_root(config_root: Path) -> Path:
    root = config_root / ROOT_DIR
    resolved = root.resolve()
    if resolved == Path("/tmp") or Path("/tmp") in resolved.parents:
        raise RuntimeError(f"role workspaces never live under /tmp (tmpfs, emptied on reboot): {resolved}")
    return root


def _write_prompt(workspace: Path, agent: RoleAgent, recorded_sha: str | None) -> str:
    """AGENTS.md of a role workspace: written when absent or still the kit's last text; an edited file is kept."""
    target = workspace / "AGENTS.md"
    new = agent["prompt"]
    if target.is_file():
        try:
            current = target.read_text(encoding="utf-8")
        except OSError:
            return "kept"
        # The kit's own managed block (added by the workspace-block step) is not an operator edit.
        lines = current.splitlines(keepends=True)
        begin, end = _shared._managed_block_span(lines)
        if begin is not None and end is not None:
            current = "".join(lines[:begin] + lines[end + 1:]).lstrip("\r\n")
        if current == new:
            return "same"
        if recorded_sha is None or hashlib.sha256(current.encode("utf-8")).hexdigest() != recorded_sha:
            return "kept"
    workspace.mkdir(parents=True, exist_ok=True)
    target.write_text(new, encoding="utf-8")
    return "written"


def configure(s: "state.SetupState", doc: Mapping[str, Any], *, config_root: Path,
              by_role: Mapping[str, Any], by_persona: Mapping[str, Any] | None, ak_path: str, kit_root: Path,
              enabled: Iterable[str] | None, catalog: Mapping[str, Iterable[str]] | None,
              effort_table: Mapping[str, str] | None, methods: Mapping[str, str],
              apply_patch: Callable[..., "tuple[bool, str]"],
              register_agent: Callable[[str, Path, str], str],
              classify: Callable[[dict], list[str]],
              change_records: Callable[[dict, dict, Any, str], list[dict]],
              run_changes: list[dict] | None = None, dry_run: bool = False,
              info: Callable[[str], None] = print, warn: Callable[[str], None] = print,
              error: Callable[[str], None] = print, env: dict | None = None,
              cli_runtimes: Iterable[str] = ()) -> bool:
    """Write (or update, fill-only) the kit-role agents. True when the config changed. Nothing is written in a dry run."""
    o = s.openclaw
    if enabled is None:
        return False                          # the operator never chose providers: the kit routes nothing here
    agents, fell = build_agents(kit_root, by_role, enabled=enabled, catalog=catalog, effort_table=effort_table,
                                ak_path=ak_path, by_persona=by_persona, env=env)
    if not agents:
        return False
    for role, want, ref in fell:
        warn(f"kit-role {role}: provider {want} is not enabled; it runs on {ref}")
    try:
        root = workspace_root(config_root)
    except RuntimeError as e:
        error(str(e))
        return False
    recorded = copy.deepcopy(o.role_agents_applied) if o.role_agents_applied else {}
    workspaces = {a.id: root / a.id for a in agents}
    cli_runtimes = tuple(cli_runtimes)
    patch, kept = build_patch(agents, doc, workspaces, recorded, methods, cli_runtimes)
    for aid in kept:
        warn(f"kit-role agent {aid}: your model or thinking setting is kept (the kit changes only values it wrote)")
    entries = _get(doc, "agents", "entries") or {}
    new_ids = [a.id for a in agents if a.id not in entries]
    if dry_run:
        info(f"Kit roles (dry run): {len(new_ids)} new agent(s), "
             f"{len(_leaf_count(patch))} config value(s) to fill")
        return False
    if patch:
        ok, out = apply_patch(patch, dry_run=True)
        if not ok:
            error(f"OpenClaw rejected the kit-role patch: {ui.safe_excerpt(out, 300, tail=True)}")
            return False
        restart = classify(patch)
        if restart:
            recorded["deferred"] = sorted(restart)
            o.role_agents_applied = recorded
            warn("Kit-role keys need a gateway restart and were NOT written (the kit never restarts a live gateway): "
                 + ", ".join(sorted(restart)))
            return False
    recorded.pop("deferred", None)
    created = list(recorded.get("created_agent_ids") or [])
    created_ws = list(recorded.get("created_workspaces") or [])
    shas = dict(recorded.get("prompt_sha256") or {})
    ready: list[str] = []
    if new_ids:
        # each `openclaw agents add` hot-reloads the gateway (~20 s): without a line per agent this looks like a hang
        info(f"Kit roles: adding {len(new_ids)} agent(s); each one reloads the gateway, about 20 s apiece")
    added = 0
    for agent in agents:
        aid = agent.id
        ws = workspaces[aid]
        existed = ws.exists()
        state_ = _write_prompt(ws, agent, shas.get(aid))
        if state_ in ("written", "same"):
            shas[aid] = agent["prompt_sha256"]
        elif state_ == "kept":
            warn(f"kit-role agent {aid}: your edited AGENTS.md is kept")
        if aid in entries:
            ready.append(aid)
            continue
        added += 1
        info(f"  ({added}/{len(new_ids)}) {aid}")
        status = register_agent(aid, ws, agent["model"])
        if status == "added":
            created.append(aid)
            if not existed:
                created_ws.append(str(ws))
            ready.append(aid)
        elif status == "existing":
            ready.append(aid)
        else:
            error(f"could not add kit-role agent {aid}: {ui.safe_text(status)}")
    doc2 = copy.deepcopy(dict(doc))
    ents = doc2.setdefault("agents", {}).setdefault("entries", {})
    for aid in ready:
        ents.setdefault(aid, {})
    live_agents = [a for a in agents if a.id in ready]
    patch2, _kept = build_patch(live_agents, doc2, workspaces, {**recorded, "created_agent_ids": created}, methods,
                                cli_runtimes)
    # A freshly registered agent only needs its fields filled: the entry `agents add` made is empty of ours.
    for a in live_agents:
        if a.id in new_ids and a.id in ready:
            patch2.setdefault("agents", {}).setdefault("entries", {})[a.id] = {
                k: v for k, v in _entry(a, workspaces[a.id], runtime_for(a, methods, cli_runtimes)).items()
                if k != "name"}
    changed = bool(created and set(created) - set(recorded.get("created_agent_ids") or []))
    if patch2:
        ok, out = apply_patch(patch2)
        if not ok:
            error(f"OpenClaw rejected the kit-role patch: {ui.safe_excerpt(out, 300, tail=True)}")
        else:
            changed = True
            if run_changes is not None:
                run_changes.extend(change_records(dict(doc2), patch2, None, "roles"))
            models = dict(recorded.get("models") or {})
            thinking = dict(recorded.get("thinking") or {})
            for a in live_agents:
                if a.id in ready:
                    models[a.id] = a["model"]
                    thinking[a.id] = a["thinking"]
            recorded["models"], recorded["thinking"] = models, thinking
            cat = sorted(set(recorded.get("catalog_added") or [])
                         | set(((patch2.get("agents") or {}).get("defaults") or {}).get("models") or {}))
            recorded["catalog_added"] = cat
            if cat and "catalog_container_absent" not in recorded:
                recorded["catalog_container_absent"] = _get(doc, "agents", "defaults", "models") is None
    else:
        models = dict(recorded.get("models") or {})
        thinking = dict(recorded.get("thinking") or {})
        for a in live_agents:
            models.setdefault(a.id, a["model"])
            thinking.setdefault(a.id, a["thinking"])
        recorded["models"], recorded["thinking"] = models, thinking
    recorded["created_agent_ids"] = sorted(set(created))
    recorded["created_workspaces"] = sorted(set(created_ws))
    recorded["prompt_sha256"] = shas
    recorded["workspace_root"] = str(root)
    o.role_agents_applied = {k: v for k, v in recorded.items() if v not in ({}, [], None)}
    if changed:
        info("Kit roles applied: " + ", ".join(sorted(ready)))
    return changed


def _leaf_count(patch: Mapping[str, Any]) -> list[str]:
    out: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping) and node:
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        else:
            out.append(path)
    walk(patch, "")
    return out


def teardown(s: "state.SetupState", doc: Mapping[str, Any], *, apply_patch: Callable[..., "tuple[bool, str]"],
             oc: Callable[..., "tuple[int, str]"], confirm: Callable[[str], bool] = lambda _m: True,
             remove_dir: Callable[[Path], None] | None = None,
             info: Callable[[str], None] = print, error: Callable[[str], None] = print) -> bool:
    """Remove only what the kit recorded: catalog refs it added, agents it created (after confirmation) and their
    workspaces. An id that is not `kit-role-*` is never touched."""
    o = s.openclaw
    applied = o.role_agents_applied or {}
    if not applied:
        return False
    catalog = _get(doc, "agents", "defaults", "models") or {}
    removed = [r for r in applied.get("catalog_added") or [] if r in catalog]
    if removed:
        frag: dict[str, Any] = {"agents": {"defaults": {"models": {r: None for r in removed}}}}
        if applied.get("catalog_container_absent") and set(catalog) <= set(removed):
            frag = {"agents": {"defaults": {"models": None}}}
        ok, out = apply_patch(frag)
        if not ok:
            error(f"OpenClaw kit-role teardown failed: {ui.safe_excerpt(out, 300, tail=True)}")
            return False
    remaining: list[str] = []
    for aid in applied.get("created_agent_ids") or []:
        if not is_kit_role_id(aid):
            continue
        if not confirm(f"Delete the kit-role agent {aid} that setup created?"):
            remaining.append(aid)
            continue
        rc, out = oc(["agents", "delete", aid, "--force"])
        if rc != 0:
            error(f"could not delete kit-role agent {aid}: {ui.safe_excerpt(out, 200, tail=True)}")
            remaining.append(aid)
    import shutil
    rm = remove_dir or (lambda p: shutil.rmtree(p, ignore_errors=True))
    root = Path(str(applied.get("workspace_root") or ""))
    kept_ws: list[str] = []
    for ws in applied.get("created_workspaces") or []:
        p = Path(ws)
        if p.name in remaining or p.is_symlink() or not is_kit_role_id(p.name) or root == Path("") \
                or root.resolve() not in p.resolve().parents:
            kept_ws.append(ws)
            continue
        rm(p)
    if remaining or kept_ws:
        o.role_agents_applied = {"created_agent_ids": remaining, "created_workspaces": kept_ws,
                                 "workspace_root": str(root)}
        return True
    o.role_agents_applied = {}
    info("Kit-role agents removed (only kit-role ids).")
    return True
