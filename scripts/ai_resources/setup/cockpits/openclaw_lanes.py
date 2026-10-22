"""OpenClaw writer for subscription lane agents (ADR-0007, plan 4.5). Operator-run only.

What it writes, and only this:

* `agents.entries.<kit-lane-*>`, through OpenClaw's own `agents add` (`openclaw_host.agent_new`, the second
  validated writer), after the patch is classified with `openclaw_reload_rules`;
* `agents.defaults.models.<ref>` catalog entries that are absent (fill-only);
* the lane ids in `agents.entries.<orchestrator>.subagents.allowAgents`, on the named orchestrators only
  (`agents.defaults` is never touched).

Setup never spawns a session and never calls the live gateway: `verify_capability` is derived from the
static config. Teardown removes only what `OpenClawState.lanes_applied` records.

Antigravity (ADR-0007 section 11): an `agy-cli-lane` agent is written only when `writable_backends()` includes
its backend (every phase 3 gate proven) and `lanes_render.agy_admission` admits it (constant, flag, valid Google
acknowledgement listing the role) and the kit plugin has an absolute `agyBin`. The writer adds the catalog stub
`models.providers.agy-cli-lane`, the tool denials and, when G5 is proven, the kit-owned agy home path. A
previously written agy agent that is no longer admitted is pruned: `allowAgents` ids, deny tokens and the stub
model leave unconditionally, then `agents delete` and the workspace removal ask for confirmation.
"""
from __future__ import annotations

import copy
import fnmatch
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ... import agy_lane_gates
from .. import state, ui

PREFIX = "kit-lane-"
CLAUDE_CLI = "claude-cli"
KIT_LANES_DIR = "kit-lanes"
_EXEC_OFF = {"", "off", "deny", "none", "disabled"}
_EXEC_DENY_TOKENS = {"exec", "mcp__openclaw__exec", "group:runtime"}
# OpenClaw tool policy (docs/gateway/sandbox-vs-tool-policy-vs-elevated.md, "Tool groups"): `group:runtime`
# expands to exec, process and code_execution; entries are `*`-globs over tool ids and group names, so
# `group:*` and `*` cover `group:runtime`.
_EXEC_NAMES = ("exec", "mcp__openclaw__exec", "group:runtime")


def _matches_exec(pattern: str) -> bool:
    """A `tools.deny` / `tools.allow` entry that names exec: exact, group:runtime or a glob such as `*`."""
    p = pattern.strip().lower()
    return any(fnmatch.fnmatchcase(n, p) for n in _EXEC_NAMES)


class LanesWriteError(RuntimeError):
    """The lanes patch could not be built or applied."""


def _get(doc: Any, *keys: str) -> Any:
    cur = doc
    for k in keys:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(k)
    return cur


# The base set of backends setup writes an agent for. `agy-cli-lane` joins it only through
# `writable_backends()`, when every phase 3 gate (agy_lane_gates G0..G7) is proven: G4 (V9) unproven keeps the
# lane out of the writer, exactly as the plan's stop rule says.
WRITABLE_BACKENDS = frozenset({CLAUDE_CLI})
# The agent runtime a catalog entry names per backend. `codex-cli` is the PROPOSED shape, recorded for the
# day the Codex lane is accepted: it is not written (not in WRITABLE_BACKENDS) and is not verified against a
# live OpenClaw (ADR-0007 FU-2, "not proven").
CATALOG_SHAPES = {CLAUDE_CLI: {"agentRuntime": {"id": CLAUDE_CLI}},
                  "codex-cli": {"agentRuntime": {"id": "codex-cli"}}}
AGY_BACKEND = agy_lane_gates.AGY_LANE_BACKEND
HELD_BACKEND_REASON = "held: backend {backend} is not writable in this version"
HELD_AGY_BIN_REASON = ("held: agy binary not configured for the kit plugin (plugins.entries.ai-resources."
                       "config.agyBin must be an absolute path); run setup in a drained window")
STUB_PROVIDER = "agy-cli-lane"
STUB_BASE_URL = "http://127.0.0.1:9/"
STUB_API = "openai-completions"          # the schema value is not proven (isolated run pending): see ADR-0007 11
STUB_API_KEY = "kit-lane-agy-placeholder"      # a constant, never a credential
# The OpenClaw tool names an agy lane child must not hold: the plan's minimum until V7 enumerates them.
AGY_TOOL_DENY = ("message", "sessions_spawn", "cron")
AGY_HOME_PARTS = agy_lane_gates.AGY_HOME_PARTS


def writable_backends() -> frozenset:
    """`WRITABLE_BACKENDS`, plus `agy-cli-lane` when every phase 3 gate is proven (fail closed)."""
    if agy_lane_gates.all_proven():
        return WRITABLE_BACKENDS | {AGY_BACKEND}
    return WRITABLE_BACKENDS


def _agy_bin_ok(doc: Mapping[str, Any] | None) -> bool:
    raw = _get(doc, "plugins", "entries", "ai-resources", "config", "agyBin") if doc is not None else None
    return isinstance(raw, str) and raw.startswith("/")


def held_agents(lanes: Mapping[str, Any], *, gates: Mapping[str, Any] | None = None,
                acks: Mapping[str, Any] | None = None, doc: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Kit ids in the state the writer does not write, with the reason.

    A backend that is not writable is held. An agy agent must also pass `lanes_render.agy_admission` (so a
    state written by an older kit, or one whose acknowledgement went stale, is held at write time) and, when
    the config is known, the plugin must carry an absolute `agyBin`.
    """
    from .. import lanes_render
    out: dict[str, str] = {}
    writable = writable_backends()
    gates = (lanes.get("gates") or {}) if gates is None else gates
    admission = lanes_render.agy_admission(lanes, gates, acks)
    for aid, agent in sorted((lanes.get("agents") or {}).items()):
        if not (isinstance(aid, str) and aid.startswith(PREFIX)):
            continue
        backend = (agent or {}).get("backend") if isinstance(agent, Mapping) else None
        if backend not in writable:
            why = HELD_BACKEND_REASON.format(backend=backend or "?")
            if backend == AGY_BACKEND:
                why += "; " + (agy_lane_gates.refusal() or "")
            out[aid] = why
        elif backend != CLAUDE_CLI and aid in admission:
            out[aid] = admission[aid]
        elif backend != CLAUDE_CLI and doc is not None and not _agy_bin_ok(doc):
            out[aid] = HELD_AGY_BIN_REASON
    return out


def kit_agent_ids(lanes: Mapping[str, Any], *, gates: Mapping[str, Any] | None = None,
                  acks: Mapping[str, Any] | None = None, doc: Mapping[str, Any] | None = None) -> list[str]:
    """Only kit ids are ever written or removed: an agent not named `kit-lane-*` is never ours, and an agent
    whose backend is not writable or not admitted is held."""
    held = held_agents(lanes, gates=gates, acks=acks, doc=doc)
    return sorted(a for a in (lanes.get("agents") or {})
                  if isinstance(a, str) and a.startswith(PREFIX) and a not in held)


agy_home_path = agy_lane_gates.agy_home_path


def agy_home_wanted() -> bool:
    """True only when V12 (G5) is proven: the kit-owned home may then be created."""
    return agy_lane_gates.gate_proven("G5")


def ensure_agy_home(path: Path) -> dict[str, Any]:
    """Create the kit-owned agy home (0700); never under /tmp or through a symlink."""
    resolved = path.resolve()
    if resolved == Path("/tmp") or Path("/tmp") in resolved.parents:
        raise LanesWriteError(f"the agy home never lives under /tmp: {resolved}")
    if path.is_symlink():
        raise LanesWriteError(f"the agy home is a symlink: {path}")
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    import os
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
    import shutil
    (remove_dir or (lambda p: shutil.rmtree(p, ignore_errors=True)))(path)
    return True


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
    explicit grant -> `exec` by reference to the isolated V8 evidence; anything else -> `none`. An explicit deny
    (`tools.exec.mode` off, a `tools.deny` entry naming exec or a glob covering it) and a non-empty `tools.allow`
    that leaves exec out (counting `alsoAllow` as additive) are `none`; `group:*` and `*` globs cover
    `group:runtime`. Not decided statically: tool profiles (`tools.profile`) and plugin-provided
    grants.
    """
    out: dict[str, dict[str, str]] = {}
    entries = _get(doc, "agents", "entries") or {}
    for orch in sorted(set(orchestrators)):
        entry = entries.get(orch) if isinstance(entries, Mapping) else None
        entry = entry if isinstance(entry, Mapping) else {}
        mode = _get(entry, "tools", "exec", "mode")
        allow = list(_get(entry, "tools", "allow") or []) + list(_get(entry, "tools", "alsoAllow") or [])
        deny = [d for d in (_get(entry, "tools", "deny") or []) if isinstance(d, str)]
        exec_denied = ((isinstance(mode, str) and mode.lower() in _EXEC_OFF and mode != "")
                       or any(_matches_exec(d) for d in deny))
        base_allow = [x for x in (_get(entry, "tools", "allow") or []) if isinstance(x, str)]
        extra_allow = [x for x in (_get(entry, "tools", "alsoAllow") or []) if isinstance(x, str)]
        # `alsoAllow` adds to the allowlist: an allowlist excludes exec only when neither list names it
        allow_excludes_exec = bool(base_allow) and not any(_matches_exec(x) for x in base_allow + extra_allow)
        allow_names_exec = any(_matches_exec(x) for x in base_allow + extra_allow)
        if exec_denied:                     # an explicit deny is never overridden by the isolated evidence
            out[orch] = {"mode": "none", "source": "static-config", "evidence_ref": ""}
        elif any(isinstance(a, str) and "lanes" in a for a in allow):
            out[orch] = {"mode": "plugin-tool", "source": "static-config", "evidence_ref": ""}
        elif allow_excludes_exec:           # an explicit allowlist that leaves exec out is a deny, even next to a mode (fail closed)
            out[orch] = {"mode": "none", "source": "static-config", "evidence_ref": ""}
        elif (isinstance(mode, str) and mode.lower() not in _EXEC_OFF) or allow_names_exec:
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


BUNDLE_MCP = "bundle-mcp"
_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def safe_server_name(raw: str) -> str:
    """OpenClaw's model-facing server name (`sanitizeServerName`): unsafe chars -> `-`, a letter first, <= 30."""
    base = _SAFE_RE.sub("-", raw.strip()) or "mcp"
    if not re.match(r"[A-Za-z]", base):
        base = f"mcp-{base}"
    return base[:30]


def mcp_deny(agent: Mapping[str, Any], doc: Mapping[str, Any]) -> list[str]:
    """The `tools.deny` tokens that scope a lane agent's MCP servers (docs: whole-server denials exclude the
    server before it is spawned). Default: `bundle-mcp` (every bundled/configured server). With an explicit
    per-role opt-in (`mcp_servers`), the configured servers outside it are denied by name instead."""
    if agent.get("backend") == AGY_BACKEND:
        return [BUNDLE_MCP, *AGY_TOOL_DENY]          # no per-server opt-in on an agy lane (V7 unproven)
    keep = {str(n) for n in (agent.get("mcp_servers") or []) if isinstance(n, str)}
    if not keep:
        return [BUNDLE_MCP]
    configured = _get(doc, "mcp", "servers") or {}
    names = sorted({safe_server_name(n) for n in configured if isinstance(n, str) and n not in keep})
    return [f"{n}__*" for n in names]


AGY_AGENT_PREFIX = PREFIX + "agy-"


def agy_primary_drift(doc: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(agent id, primary ref) for every `kit-lane-agy-*` agent in the live config whose primary is an Anthropic model.

    An agy lane is meant to run on the Antigravity subscription (`agy-cli-lane/*`). One that runs a Claude model
    on the API bills per token (the 2026-10-22 analysis found Opus 5.5 on three of them). The kit only REPORTS this:
    it does not repoint the agent, and it leaves the agy lane gates and held agents exactly as they are (ADR-0007
    section 11: the gates stay unproven)."""
    entries = _get(doc, "agents", "entries") or {}
    out: list[tuple[str, str]] = []
    if not isinstance(entries, Mapping):
        return out
    for aid in sorted(entries):
        if not (isinstance(aid, str) and aid.startswith(AGY_AGENT_PREFIX)):
            continue
        model = (entries[aid] or {}).get("model") if isinstance(entries[aid], Mapping) else None
        primary = model.get("primary") if isinstance(model, Mapping) else model
        if isinstance(primary, str) and (primary.startswith("anthropic/") or primary.startswith("claude-")):
            out.append((aid, primary))
    return out


def agy_primary_drift_findings(doc: Mapping[str, Any]) -> list[str]:
    return [f"{aid} is an agy lane but its primary is {ref} on the Anthropic API (billed per token, not the "
            "Antigravity subscription). The kit does not repoint it: agy lanes stay closed until the ADR-0007 "
            "gates are proven. Repoint or remove the agent by hand if the spend is unwanted."
            for aid, ref in agy_primary_drift(doc)]


def mcp_scope_findings(lanes: Mapping[str, Any], doc: Mapping[str, Any],
                       acks: Mapping[str, Any] | None = None) -> list[str]:
    """One message per kit lane agent present in `doc` whose `tools.deny` lacks the scoping tokens."""
    entries = _get(doc, "agents", "entries") or {}
    out: list[str] = []
    for aid in kit_agent_ids(lanes, acks=acks, doc=doc):
        entry = entries.get(aid) if isinstance(entries, Mapping) else None
        if not isinstance(entry, Mapping):
            continue
        have = list(_get(entry, "tools", "deny") or [])
        missing = [t for t in mcp_deny(lanes["agents"][aid], doc) if t not in have]
        if missing:
            out.append(f"lane agent {aid} is not MCP-scoped (tools.deny lacks {', '.join(missing)}): it inherits "
                       "every configured MCP server")
    return out


def agy_slug(agent: Mapping[str, Any]) -> str:
    model = str(agent.get("model") or "")
    return model.split("/", 1)[1] if model.startswith(f"{STUB_PROVIDER}/") else model


def _stub_patch(agents: Mapping[str, Any], wanted: list[str], doc: Mapping[str, Any]) -> dict[str, Any] | None:
    """The `models.providers.agy-cli-lane` stub for the admitted agy agents (fill-only), or None.

    ADR-0005 finding 1: `sessions_spawn` refuses `Unknown model` without a catalog entry. The key is the
    placeholder constant; an existing provider entry keeps every operator value and only gains missing models.
    """
    slugs = [agy_slug(agents[a]) for a in wanted if agents[a].get("backend") == AGY_BACKEND]
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


def build_patch(lanes: Mapping[str, Any], doc: Mapping[str, Any], workspaces: Mapping[str, Path], *,
                ids: Iterable[str] | None = None,
                mcp_prev: Mapping[str, Iterable[str]] | None = None) -> dict[str, Any]:
    """The fill-only lanes patch for `doc`. Empty when nothing is missing.

    Entries are only added for ids absent from the config; the catalog only for refs absent from
    `agents.defaults.models`; `allowAgents` receives the ids missing from each NAMED orchestrator that
    exists in the config. `agents.defaults.subagents` and every other agent are never touched.
    """
    agents = lanes.get("agents") or {}
    wanted = ([i for i in ids if isinstance(i, str) and i.startswith(PREFIX) and i in agents]
              if ids is not None else kit_agent_ids(lanes))
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
        # MCP scoping (V7): kit-owned tokens in tools.deny; operator tokens are kept, stale kit tokens dropped.
        existing = entries.get(aid) if isinstance(entries, Mapping) else None
        current = list(_get(existing or {}, "tools", "deny") or [])
        desired = mcp_deny(agents[aid], doc)
        prev = set((mcp_prev or {}).get(aid) or [])
        stale = [t for t in current if t in prev and t not in desired]
        missing = [t for t in desired if t not in current]
        if stale or missing:
            merged = [t for t in current if t not in stale] + missing
            ent = patch.setdefault("agents", {}).setdefault("entries", {}).setdefault(aid, {})
            ent.setdefault("tools", {})["deny"] = merged if merged or not stale else None
    stub = _stub_patch(agents, wanted, doc)
    if stub:
        patch.setdefault("models", {}).setdefault("providers", {})[STUB_PROVIDER] = stub
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


def restore_fragment(applied: Mapping[str, Any], doc: Mapping[str, Any], *,
                     only: Iterable[str] | None = None) -> tuple[dict[str, Any], list[str]]:
    """The patch that removes exactly what the kit added, from what is live now.

    `allowAgents` by set difference (an id the operator added later stays); catalog refs the kit added are
    deleted; the agy catalog stub models the kit added are removed (the entry and its container too when the
    kit created them and nothing else is in them); agent entries are not here: `agents delete` removes them
    (and only recorded ids). With `only`, just those agent ids are restored (the revocation prune).
    """
    patch: dict[str, Any] = {}
    entries = _get(doc, "agents", "entries") or {}
    absent = set(applied.get("allow_agents_absent") or [])
    scope = None if only is None else set(only)
    for orch, ids in sorted((applied.get("allow_agents_added") or {}).items()):
        entry = entries.get(orch) if isinstance(entries, Mapping) else None
        if not isinstance(entry, Mapping):
            continue
        if scope is not None:
            ids = [i for i in ids if i in scope]
            if not ids:
                continue
        live = list(_get(entry, "subagents", "allowAgents") or [])
        kept = [x for x in live if x not in set(ids)]
        if kept == live:
            continue
        value: Any = kept if (kept or orch not in absent) else None
        patch.setdefault("agents", {}).setdefault("entries", {}).setdefault(orch, {}) \
            .setdefault("subagents", {})["allowAgents"] = value
    for aid, tokens in sorted((applied.get("mcp_deny_added") or {}).items()):
        if scope is not None and aid not in scope:
            continue
        entry = entries.get(aid) if isinstance(entries, Mapping) else None
        if not isinstance(entry, Mapping):
            continue
        live = list(_get(entry, "tools", "deny") or [])
        kept = [x for x in live if x not in set(tokens)]
        if kept != live:
            patch.setdefault("agents", {}).setdefault("entries", {}).setdefault(aid, {}) \
                .setdefault("tools", {})["deny"] = kept or None
    stub_slugs = {aid: slug for aid, slug in (applied.get("providers_added") or {}).items()
                  if scope is None or aid in scope}
    provider = _get(doc, "models", "providers", STUB_PROVIDER)
    if stub_slugs and isinstance(provider, Mapping):
        live_models = [m for m in (provider.get("models") or []) if isinstance(m, Mapping)]
        kept_models = [m for m in live_models if m.get("id") not in set(stub_slugs.values())]
        if len(kept_models) != len(live_models):
            patch.setdefault("models", {}).setdefault("providers", {})
            if not kept_models and applied.get("provider_entry_absent", True):
                patch["models"]["providers"][STUB_PROVIDER] = None
                others = set(_get(doc, "models", "providers") or {}) - {STUB_PROVIDER}
                if applied.get("provider_container_absent") and not others:
                    patch["models"]["providers"] = None
            else:
                patch["models"]["providers"][STUB_PROVIDER] = {"models": kept_models}
    if scope is not None:
        return patch, []
    catalog = _get(doc, "agents", "defaults", "models") or {}
    removed = [ref for ref in applied.get("catalog_added") or [] if ref in catalog]
    for ref in removed:
        patch.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {})[ref] = None
    if removed and applied.get("catalog_container_absent") and set(catalog) <= set(removed):
        patch["agents"]["defaults"]["models"] = None      # the kit created the container; nothing else is in it
    return patch, []


def _recorded_agy_ids(applied: Mapping[str, Any]) -> set[str]:
    rec = set(applied.get("created_agent_ids") or [])
    for ids in (applied.get("allow_agents_added") or {}).values():
        rec |= set(ids or [])
    rec |= set(applied.get("mcp_deny_added") or {}) | set(applied.get("providers_added") or {})
    return {i for i in rec if isinstance(i, str) and i.startswith(PREFIX + "agy-")}


def _prune_revoked(s: "state.SetupState", doc: Mapping[str, Any], revoked: list[str], *,
                   root: Path | None, apply_patch: Callable[..., "tuple[bool, str]"],
                   delete_agent: Callable[[str], "tuple[int, str]"] | None, confirm: Callable[[str], bool],
                   dry_run: bool, info: Callable[[str], None], error: Callable[[str], None]) -> bool:
    """A previously written agy agent that is no longer admitted (flag off, ack revoked or stale) is torn down in
    teardown's order: first, unconditionally and in one patch, its `allowAgents` ids, its kit deny tokens and
    its stub model; only then, with confirmation, `agents delete` and the workspace. A declined delete leaves an
    entry no orchestrator may spawn and whose model is no longer in the catalog (`Unknown model` at spawn)."""
    o = s.openclaw
    applied = copy.deepcopy(o.lanes_applied) if o.lanes_applied else {}
    frag, _ = restore_fragment(applied, doc, only=revoked)
    if dry_run:
        info("Subscription lanes (dry run): revoked agy agents would be pruned: " + ", ".join(revoked))
        return False
    if frag:
        ok, out = apply_patch(frag)
        if not ok:
            error(f"OpenClaw lanes revocation failed: {ui.safe_excerpt(out, 300, tail=True)}")
            return False
    gone = set(revoked)
    added = {orch: [i for i in ids if i not in gone] for orch, ids in (applied.get("allow_agents_added") or {}).items()}
    applied["allow_agents_added"] = {k: v for k, v in added.items() if v}
    for key in ("mcp_deny_added", "tool_deny_added", "providers_added"):
        applied[key] = {k: v for k, v in (applied.get(key) or {}).items() if k not in gone}
    if not applied.get("providers_added"):
        applied.pop("provider_container_absent", None)
        applied.pop("provider_entry_absent", None)
    created = list(applied.get("created_agent_ids") or [])
    created_ws = list(applied.get("created_workspaces") or [])
    import shutil
    for aid in revoked:
        if aid not in created:
            continue
        if delete_agent is None or not confirm(f"Delete the revoked lane agent {aid} that setup created?"):
            info(f"lane agent {aid}: revoked, entry kept by operator choice (not in any allowAgents)")
            continue
        rc, out = delete_agent(aid)
        if rc != 0:
            error(f"could not delete lane agent {aid}: {ui.safe_excerpt(out, 200, tail=True)}")
            continue
        created.remove(aid)
        for ws in list(created_ws):
            if Path(ws).name == aid and _contained(ws, root) is None:
                shutil.rmtree(ws, ignore_errors=True)
                created_ws.remove(ws)
    applied["created_agent_ids"] = created
    applied["created_workspaces"] = created_ws
    o.lanes_applied = {k: v for k, v in applied.items() if v not in ({}, [], None)}
    return True


def configure(s: "state.SetupState", doc: Mapping[str, Any], *, config_root: Path,
              apply_patch: Callable[..., "tuple[bool, str]"],
              register_agent: Callable[[str, Path, str], str],
              classify: Callable[[dict], list[str]],
              change_records: Callable[[dict, dict, Any, str], list[dict]],
              run_changes: list[dict] | None = None, dry_run: bool = False,
              info: Callable[[str], None] = print, warn: Callable[[str], None] = print,
              error: Callable[[str], None] = print,
              confirm: Callable[[str], bool] = lambda _m: True,
              delete_agent: Callable[[str], "tuple[int, str]"] | None = None,
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
    acks = s.profile.lane_acks
    gates = lanes.get("gates") or {}
    ids = kit_agent_ids(lanes, gates=gates, acks=acks, doc=doc)
    for aid, reason in held_agents(lanes, gates=gates, acks=acks, doc=doc).items():
        warn(f"lane agent {aid} not written: {reason}")
    try:
        root = workspace_root(lanes, config_root)
    except LanesWriteError as e:
        error(str(e))
        return False
    pruned = False
    revoked = sorted(_recorded_agy_ids(o.lanes_applied or {}) - set(ids))
    if revoked:
        pruned = _prune_revoked(s, doc, revoked, root=root, apply_patch=apply_patch, delete_agent=delete_agent,
                                confirm=confirm, dry_run=dry_run, info=info, error=error)
        if pruned:
            doc = _doc_without(doc, revoked)
    if not ids:
        return pruned
    workspaces = {i: root / i for i in ids}
    mcp_prev = (o.lanes_applied or {}).get("mcp_deny_added") or {}
    patch = build_patch(lanes, doc, workspaces, ids=ids, mcp_prev=mcp_prev)
    lanes["verify_capability"] = verify_capability(doc, lanes.get("orchestrators") or [])
    if dry_run:
        info("Subscription lanes (dry run): " + (", ".join(sorted(_leaf_paths(patch))) or "no change"))
        return False
    stub_before = {m.get("id") for m in (_get(doc, "models", "providers", STUB_PROVIDER, "models") or [])
                   if isinstance(m, Mapping)}
    entries = _get(doc, "agents", "entries") or {}
    new_ids = [i for i in ids if i not in entries]
    if patch:
        ok, out = apply_patch(patch, dry_run=True)
        if not ok:
            error(f"OpenClaw rejected the lanes patch: {ui.safe_excerpt(out, 300, tail=True)}")
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
            error(f"could not add lane agent {aid}: {ui.safe_text(status)}")
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
    patch2 = build_patch(lanes, doc2, workspaces, ids=ready, mcp_prev=mcp_prev)
    changed = bool(new_ids and created) or pruned
    if patch2:
        ok, out = apply_patch(patch2)
        if not ok:
            error(f"OpenClaw rejected the lanes patch: {ui.safe_excerpt(out, 300, tail=True)}")
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
            scoped = dict(applied.get("mcp_deny_added") or {})
            for aid, ent in ((patch2.get("agents") or {}).get("entries") or {}).items():
                if aid not in ready or "tools" not in ent:
                    continue
                live = set(_get(entries.get(aid) or {}, "tools", "deny") or [])
                new = [t for t in (ent["tools"].get("deny") or []) if t not in live]
                kept = [t for t in scoped.get(aid, []) if t in (ent["tools"].get("deny") or [])]
                merged = sorted(set(kept) | set(new))
                if merged:
                    scoped[aid] = merged
                else:
                    scoped.pop(aid, None)
            applied["mcp_deny_added"] = scoped
            tool_deny = dict(applied.get("tool_deny_added") or {})
            for aid in ready:
                if (lanes["agents"].get(aid) or {}).get("backend") == AGY_BACKEND and scoped.get(aid):
                    tool_deny[aid] = sorted(t for t in scoped[aid] if t in AGY_TOOL_DENY)
            applied["tool_deny_added"] = tool_deny
            new_models = (((patch2.get("models") or {}).get("providers") or {}).get(STUB_PROVIDER) or {}) \
                .get("models") or []
            added_slugs = {m.get("id") for m in new_models if isinstance(m, Mapping)} - stub_before
            if added_slugs:
                prov = dict(applied.get("providers_added") or {})
                for aid in ready:
                    ag = lanes["agents"].get(aid) or {}
                    if ag.get("backend") == AGY_BACKEND and agy_slug(ag) in added_slugs:
                        prov[aid] = agy_slug(ag)
                applied["providers_added"] = prov
                if "provider_container_absent" not in applied:
                    applied["provider_container_absent"] = _get(doc, "models", "providers") is None
                if "provider_entry_absent" not in applied:
                    applied["provider_entry_absent"] = _get(doc, "models", "providers", STUB_PROVIDER) is None
            cat = sorted(set(applied.get("catalog_added") or [])
                         | set(((patch2.get("agents") or {}).get("defaults") or {}).get("models") or {}))
            applied["catalog_added"] = cat
            if cat and "catalog_container_absent" not in applied:
                # The kit created `agents.defaults.models` itself: teardown removes the empty container too.
                applied["catalog_container_absent"] = _get(doc, "agents", "defaults", "models") is None
    if agy_home_wanted() and any((lanes["agents"].get(i) or {}).get("backend") == AGY_BACKEND for i in ready):
        prior = applied.get("agy_home")
        rec = ensure_agy_home(agy_home_path())
        applied["agy_home"] = prior if isinstance(prior, Mapping) and prior.get("created") else rec
    applied["created_agent_ids"] = sorted(set(created))
    applied["created_workspaces"] = sorted(set(created_ws))
    if created_ws:
        applied["workspace_root"] = str(root)         # teardown's containment root, even after --no-lanes
    o.lanes_applied = {k: v for k, v in applied.items() if v not in ({}, [], None)}
    if changed:
        info("Subscription lanes applied: " + ", ".join(ready))
    return changed


def _doc_without(doc: Mapping[str, Any], ids: list[str]) -> dict[str, Any]:
    """`doc` as it is after the revocation patch (the ids are out of every allowAgents list)."""
    out = copy.deepcopy(dict(doc))
    gone = set(ids)
    for entry in ((_get(out, "agents", "entries") or {}).values()):
        allow = _get(entry, "subagents", "allowAgents")
        if isinstance(allow, list):
            entry["subagents"]["allowAgents"] = [a for a in allow if a not in gone]
    return out


def _leaf_paths(patch: Mapping[str, Any], prefix: str = "") -> list[str]:
    out: list[str] = []
    for k, v in patch.items():
        path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, Mapping) and v:
            out.extend(_leaf_paths(v, path))
        else:
            out.append(path)
    return out


def _contained(ws: str, root: Path | None) -> str | None:
    """None when `ws` may be removed; otherwise the reason it is refused. Only a real (non-symlink)
    `kit-lane-*` directory whose resolved path lies strictly under the resolved workspace root."""
    p = Path(ws)
    if not p.name.startswith(PREFIX):
        return "not a kit-lane-* directory"
    if root is None:
        return "no workspace root is known"
    if p.is_symlink():
        return "is a symlink"
    try:
        rp, rr = p.resolve(), root.resolve()
    except OSError as e:
        return f"cannot be resolved ({e})"
    if rp == rr or rr not in rp.parents:
        return f"resolves outside the workspace root {rr}"
    return None


def teardown(s: "state.SetupState", doc: Mapping[str, Any], *,
             config_root: Path | None = None,
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
            error(f"OpenClaw lanes teardown failed: {ui.safe_excerpt(out, 300, tail=True)}")
            return False
    home = applied.get("agy_home")
    keep_home = None
    if isinstance(home, Mapping):
        if not remove_agy_home(home, confirm=confirm, remove_dir=remove_dir):
            keep_home = dict(home)             # declined or not removable: kept and recorded
    remaining_ids: list[str] = []
    for aid in applied.get("created_agent_ids") or []:
        if not (isinstance(aid, str) and aid.startswith(PREFIX)):
            continue                                  # never delete an id that is not a kit lane id
        if not confirm(f"Delete the lane agent {aid} that setup created?"):
            remaining_ids.append(aid)
            continue
        rc, out = oc(["agents", "delete", aid, "--force"])
        if rc != 0:
            error(f"could not delete lane agent {aid}: {ui.safe_excerpt(out, 200, tail=True)}")
            remaining_ids.append(aid)
    import shutil
    rm = remove_dir or (lambda p: shutil.rmtree(p, ignore_errors=True))
    root: Path | None = None
    try:
        if applied.get("workspace_root"):
            root = Path(str(applied["workspace_root"]))
        elif config_root is not None:
            root = workspace_root(s.profile.lanes if isinstance(s.profile.lanes, dict) else {}, config_root)
    except LanesWriteError:
        root = None
    kept_ws: list[str] = []
    refused = False
    for ws in applied.get("created_workspaces") or []:
        if Path(ws).name in remaining_ids:
            kept_ws.append(ws)
            continue
        why = _contained(str(ws), root)
        if why:
            error(f"refusing to remove workspace {ws}: {why}")
            kept_ws.append(ws)
            refused = True
            continue
        rm(Path(ws))
    if remaining_ids or refused or keep_home:
        o.lanes_applied = {"created_agent_ids": remaining_ids, "created_workspaces": kept_ws,
                           **({"workspace_root": str(root)} if root is not None else {}),
                           **({"agy_home": keep_home} if keep_home else {})}
        return True
    o.lanes_applied = {}
    info("Subscription lanes removed (only kit lane ids).")
    return True
