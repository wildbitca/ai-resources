"""OpenClaw writer for subscription lane agents (ADR-0007, plan 4.5). Operator-run only.

What it writes, and only this:

* `agents.entries.<kit-lane-*>`, through OpenClaw's own `agents add` (`openclaw_host.agent_new`, the second
  validated writer), after the patch is classified with `openclaw_reload_rules`;
* `agents.defaults.models.<ref>` catalog entries that are absent (fill-only);
* the lane ids in `agents.entries.<orchestrator>.subagents.allowAgents`, on the named orchestrators only
  (`agents.defaults` is never touched).

Setup never spawns a session and never calls the live gateway: `verify_capability` is derived from the
static config. Teardown removes only what `OpenClawState.lanes_applied` records.

Antigravity: an `agy-cli-lane` agent is written like any other once the kit plugin has an absolute `agyBin`; there is
no flag, acknowledgement or gate (ADR-0019). The writer adds the catalog stub `models.providers.agy-cli-lane`, the tool
denials and the kit-owned agy home path. A previously written agy agent that is no longer wanted is pruned: `allowAgents`
ids, deny tokens and the stub model leave unconditionally, then `agents delete` and the workspace removal ask for
confirmation.
"""
from __future__ import annotations

import copy
import fnmatch
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .openclaw_agy import (AGY_BACKEND, AGY_HOME_PARTS, AGY_TOOL_DENY, STUB_API, STUB_API_KEY, STUB_BASE_URL,  # noqa: F401
                           STUB_PROVIDER, LanesWriteError, agy_home_path, agy_slug,
                           ensure_agy_home, remove_agy_home, stub_models as _stub_models, stub_patch_for_slugs)
from .. import state, ui
from ..live_openclaw import _primary

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


def _get(doc: Any, *keys: str) -> Any:
    cur = doc
    for k in keys:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(k)
    return cur


# The agent runtime a catalog entry names per backend.
CATALOG_SHAPES = {CLAUDE_CLI: {"agentRuntime": {"id": CLAUDE_CLI}}}
HELD_AGY_BIN_REASON = ("skipped: agy binary not configured for the kit plugin (plugins.entries.ai-resources."
                       "config.agyBin must be an absolute path); run setup in a drained window")


def _agy_bin_ok(doc: Mapping[str, Any] | None) -> bool:
    raw = _get(doc, "plugins", "entries", "ai-resources", "config", "agyBin") if doc is not None else None
    return isinstance(raw, str) and raw.startswith("/")


def skipped_agents(lanes: Mapping[str, Any], doc: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Kit ids the writer cannot write yet, with the recorded cause: an agy agent while the plugin has no absolute `agyBin`."""
    out: dict[str, str] = {}
    for aid, agent in sorted((lanes.get("agents") or {}).items()):
        if not (isinstance(aid, str) and aid.startswith(PREFIX)):
            continue
        backend = (agent or {}).get("backend") if isinstance(agent, Mapping) else None
        if backend != CLAUDE_CLI and doc is not None and not _agy_bin_ok(doc):
            out[aid] = HELD_AGY_BIN_REASON
    return out


def kit_agent_ids(lanes: Mapping[str, Any], *, doc: Mapping[str, Any] | None = None) -> list[str]:
    """Only kit ids are ever written or removed: an agent not named `kit-lane-*` is never ours."""
    skipped = skipped_agents(lanes, doc)
    return sorted(a for a in (lanes.get("agents") or {})
                  if isinstance(a, str) and a.startswith(PREFIX) and a not in skipped)


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
        elif _primary(entry.get("model")).startswith(("claude-cli/", "claude-kit/")):
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


def runtime_pins(entry: Mapping[str, Any] | None, refs: Iterable[str], shape: Mapping[str, Any]) -> dict[str, Any]:
    """`{ref: shape}` for each ref the agent's OWN `models` map does not give a runtime yet (fill-only).

    OpenClaw resolves an agent's exact model entry before `agents.defaults.models` (model-runtime-policy), so a pin here
    moves this agent only: the operator's agents that run the same ref keep the runtime they have (the API, say)."""
    own = _get(entry or {}, "models")
    own = own if isinstance(own, Mapping) else {}
    return {ref: copy.deepcopy(dict(shape)) for ref in refs
            if isinstance(ref, str) and ref and not _get(own.get(ref) or {}, "agentRuntime", "id")}


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
    it never repoints or removes anything by itself (`migrate_agy_drift` does, one confirmed agent at a time)."""
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
            "Antigravity subscription). Setup never changes it silently: it offers to point it back at "
            "agy-cli-lane/* or to remove it, one agent at a time, after your confirmation."
            for aid, ref in agy_primary_drift(doc)]


def agy_drift_proposals(doc: Mapping[str, Any], *, lanes: Mapping[str, Any] | None = None) -> list[dict[str, str]]:
    """One proposal per drifted agy lane (ADR-0015 D2b, operator answer 3). Pure: nothing is changed here.

    `repoint` needs the stub catalog to list the target model (`models.providers.agy-cli-lane`); otherwise the only
    offer is `remove`. Each proposal says why. The target is the
    lane's own model in the lanes table (`kit-lane-agy-gemini38flashhigh` -> `gemini-3.8-flash-high`); only a lane the
    table does not know falls back to the slug of the drifted Anthropic ref."""
    table = (lanes or {}).get("agents") or {}
    known = _stub_models(doc)
    out: list[dict[str, str]] = []
    for aid, ref in agy_primary_drift(doc):
        own = str(((table.get(aid) or {}) if isinstance(table, Mapping) else {}).get("model") or "")
        if own.startswith(f"{STUB_PROVIDER}/"):
            slug = own.split("/", 1)[1]
        else:
            slug = ref.split("/", 1)[1] if ref.startswith("anthropic/") else ref
        target = f"{STUB_PROVIDER}/{slug}"
        if slug in known:
            out.append({"id": aid, "action": "repoint", "from": ref, "to": target,
                        "why": "the Antigravity catalog lists this model"})
        else:
            out.append({"id": aid, "action": "remove", "from": ref, "to": "",
                        "why": f"the Antigravity catalog has no {slug}, so there is nothing to point it at"})
    return out


def migrate_agy_drift(doc: Mapping[str, Any], *, lanes: Mapping[str, Any] | None = None,
                      apply_patch: Callable[..., "tuple[bool, str]"], classify: Callable[[dict], list[str]],
                      confirm: Callable[[str], bool], delete_agent: Callable[[str], "tuple[int, str]"],
                      change_records: Callable[[dict, dict, Any, str], list[dict]] | None = None,
                      run_changes: list[dict] | None = None, interactive: bool = True, dry_run: bool = False,
                      info: Callable[[str], None] = print, warn: Callable[[str], None] = print,
                      error: Callable[[str], None] = print) -> list[dict[str, str]]:
    """The confirmed migration of drifted agy lanes. Returns the proposals that were applied.

    Never silent: a non-interactive run and a dry run only list the proposals. A repoint is one fill-only-style patch of
    `agents.entries.<id>.model.primary`, classified first (a restart-class key is not applied). A removal asks again,
    takes the id out of every `allowAgents` list in the same patch and then runs `agents delete`. Only ids that start
    with `kit-lane-agy-` are ever touched."""
    proposals = [p for p in agy_drift_proposals(doc, lanes=lanes)
                 if p["id"].startswith(AGY_AGENT_PREFIX)]
    applied: list[dict[str, str]] = []
    for p in proposals:
        line = (f"{p['id']}: {p['action']} ({p['from']}" + (f" -> {p['to']}" if p["to"] else "") + f"); {p['why']}")
        if dry_run or not interactive:
            info(f"agy lane drift ({'dry run' if dry_run else 'not asked: unattended'}): {line}")
            continue
        if not confirm(f"Apply this change? {line}"):
            info(f"agy lane drift: {p['id']} left as it is.")
            continue
        if p["action"] == "repoint":
            patch = {"agents": {"entries": {p["id"]: {"model": {"primary": p["to"]}}}}}
            restart = classify(patch)
            if restart:
                warn("agy lane drift: the repoint needs a gateway restart and was NOT applied: " + ", ".join(restart))
                continue
            ok, out = apply_patch(patch)
            if not ok:
                error(f"OpenClaw rejected the agy lane repoint: {ui.safe_excerpt(out, 300, tail=True)}")
                continue
            if change_records is not None and run_changes is not None:
                run_changes.extend(change_records(dict(doc), patch, None, "agy-drift"))
        else:
            if not confirm(f"Delete the lane agent {p['id']}? Its workspace is kept."):
                continue
            allow: dict[str, Any] = {}
            for orch, entry in ((_get(doc, "agents", "entries") or {}).items()):
                lst = _get(entry, "subagents", "allowAgents")
                if isinstance(lst, list) and p["id"] in lst:
                    allow[orch] = {"subagents": {"allowAgents": [x for x in lst if x != p["id"]]}}
            if allow:
                ok, out = apply_patch({"agents": {"entries": allow}})
                if not ok:
                    error(f"OpenClaw rejected the allowAgents cleanup: {ui.safe_excerpt(out, 300, tail=True)}")
                    continue
            rc, out = delete_agent(p["id"])
            if rc != 0:
                error(f"could not delete {p['id']}: {ui.safe_excerpt(out, 200, tail=True)}")
                continue
        applied.append(p)
        info(f"agy lane drift: applied {p['action']} to {p['id']}.")
    return applied


def mcp_scope_findings(lanes: Mapping[str, Any], doc: Mapping[str, Any]) -> list[str]:
    """One message per kit lane agent present in `doc` whose `tools.deny` lacks the scoping tokens."""
    entries = _get(doc, "agents", "entries") or {}
    out: list[str] = []
    for aid in kit_agent_ids(lanes, doc=doc):
        entry = entries.get(aid) if isinstance(entries, Mapping) else None
        if not isinstance(entry, Mapping):
            continue
        have = list(_get(entry, "tools", "deny") or [])
        missing = [t for t in mcp_deny(lanes["agents"][aid], doc) if t not in have]
        if missing:
            out.append(f"lane agent {aid} is not MCP-scoped (tools.deny lacks {', '.join(missing)}): it inherits "
                       "every configured MCP server")
    return out


def _stub_patch(agents: Mapping[str, Any], wanted: list[str], doc: Mapping[str, Any]) -> dict[str, Any] | None:
    """The agy stub for the admitted agy agents (the plumbing lives in `openclaw_agy`)."""
    return stub_patch_for_slugs([agy_slug(agents[a]) for a in wanted if agents[a].get("backend") == AGY_BACKEND], doc)


def build_patch(lanes: Mapping[str, Any], doc: Mapping[str, Any], workspaces: Mapping[str, Path], *,
                ids: Iterable[str] | None = None,
                mcp_prev: Mapping[str, Iterable[str]] | None = None) -> dict[str, Any]:
    """The fill-only lanes patch for `doc`. Empty when nothing is missing.

    Entries are only added for ids absent from the config; a claude-cli lane gets its runtime in its OWN
    `models` map when that map does not give the ref one yet (never in `agents.defaults.models`, which every
    agent shares); `allowAgents` receives the ids missing from each NAMED orchestrator that exists in the config.
    `agents.defaults` and every other agent are never touched.
    """
    agents = lanes.get("agents") or {}
    wanted = ([i for i in ids if isinstance(i, str) and i.startswith(PREFIX) and i in agents]
              if ids is not None else kit_agent_ids(lanes))
    entries = _get(doc, "agents", "entries") or {}
    patch: dict[str, Any] = {}
    for aid in wanted:
        live_entry = entries.get(aid) if isinstance(entries, Mapping) else None
        if aid not in entries:
            patch.setdefault("agents", {}).setdefault("entries", {})[aid] = _agent_entry(
                aid, agents[aid], workspaces[aid])
        cat = _catalog_entry(agents[aid])
        if cat is not None:
            pins = runtime_pins(live_entry if isinstance(live_entry, Mapping) else None,
                                [agents[aid].get("model", "")], cat)
            if pins:
                patch.setdefault("agents", {}).setdefault("entries", {}).setdefault(aid, {})["models"] = pins
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
    for aid, refs in sorted((applied.get("runtime_pins_added") or {}).items()):
        if scope is not None and aid not in scope:
            continue
        own = _get(entries.get(aid) or {}, "models") if isinstance(entries, Mapping) else None
        gone = [r for r in refs or [] if isinstance(own, Mapping) and r in own]
        if gone:
            models = patch.setdefault("agents", {}).setdefault("entries", {}).setdefault(aid, {}) \
                .setdefault("models", {})
            for ref in gone:
                models[ref] = None
    if scope is not None:
        return patch, []
    # Before 2.6.2 a claude-cli lane put its runtime in the shared catalog: what those runs recorded is still removed.
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
    ids = kit_agent_ids(lanes, doc=doc)
    for aid, reason in skipped_agents(lanes, doc).items():
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
            pinned = dict(applied.get("runtime_pins_added") or {})
            for aid, ent in ((patch2.get("agents") or {}).get("entries") or {}).items():
                if aid in ready and isinstance(ent.get("models"), Mapping):
                    pinned[aid] = sorted(set(pinned.get(aid, [])) | set(ent["models"]))
            applied["runtime_pins_added"] = pinned
    if any((lanes["agents"].get(i) or {}).get("backend") == AGY_BACKEND for i in ready):
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
