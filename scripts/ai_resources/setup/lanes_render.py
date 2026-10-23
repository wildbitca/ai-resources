"""Subscription lanes: the setup-owned table and its `lanes.yaml` rendering (ADR-0007, plan 4.1-4.2).

`ProfileState.lanes` is the WHAT. `~/.config/ai-resources/lanes.yaml` is a rendered artifact written
only when lanes are non-empty, in single-model and in multi-model mode, and by no `models ...` command
except through the fan-out re-render artifact (`model_rerender.LanesArtifact`), which re-renders it so a
moved pinned id marks lane agents stale. `executors.yaml` is not changed by any of this. Nothing here opens `openclaw.json`.

Pure helpers first (profile block -> state, drift, rendering); the one file writer (`sync_file`) is the
only function with side effects, and it backs the file up before writing it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

from .. import agy_lane_gates, model_pins, roles_policy
from ..scoring import lanes as scoring_lanes
from . import state

try:
    import yaml
except ImportError:  # pragma: no cover - handled by ui.require_deps()
    yaml = None  # type: ignore

CLAUDE_CODE = scoring_lanes.CLAUDE_CODE
CLAUDE_CLI = scoring_lanes.CLAUDE_CLI
AGENT_PREFIX = "kit-lane-"
# ADR-0007 §11 (Antigravity lane): while it is not Accepted, every `agy:` candidate is held (fail closed).
# tests/test_lanes_agy_hold.py pins this constant to the exact status line of §11 in both directions.
# Accepted is necessary and not sufficient: the flag, a valid Google acknowledgement listing the role and the
# phase 3 gates G0..G7 (agy_lane_gates) must all hold, each as an enforced check (`agy_role_reason`).
AGY_LANE_ADR_ACCEPTED = True       # ADR-0007 section 11 Accepted by the operator on 2026-10-16
HELD_AGY_REASON = "held: Antigravity lane not accepted"
# ADR-0007 follow-up FU-2 (Codex lane): registered, opt-in and HELD the same way until the operator accepts it.
# tests/test_lanes_codex_hold.py pins this False; the writer never writes a `codex-cli` agent either way.
CODEX_LANE_ADR_ACCEPTED = False
HELD_CODEX_REASON = "held: Codex lane not accepted"
CODEX_BACKEND = scoring_lanes.CODEX_CLI


def backend_held(backend: str) -> bool:
    """True when candidates and agents of `backend` are held (fail closed)."""
    if backend == CLAUDE_CLI:
        return False
    if backend == CODEX_BACKEND:
        return not CODEX_LANE_ADR_ACCEPTED
    return not AGY_LANE_ADR_ACCEPTED


def held_reason(backend: str) -> str:
    return HELD_CODEX_REASON if backend == CODEX_BACKEND else HELD_AGY_REASON


def codex_status() -> dict[str, Any]:
    """Read-only: is a `codex` binary on PATH. Never raises; a host without it reports `not installed`."""
    import shutil
    try:
        path = shutil.which("codex") or ""
    except Exception:  # noqa: BLE001
        path = ""
    summary = f"codex: {path} (lane held)" if path else "codex: not installed (lane held)"
    return {"installed": bool(path), "path": path, "held": backend_held(CODEX_BACKEND), "summary": summary}
HELD_FLAG_REASON = "held: --lanes-antigravity is off"
HELD_NO_ACK_REASON = "held: no valid Google acknowledgement (missing, stale text or no account)"
HELD_ROLE_REASON = "held: the Google acknowledgement does not list {role}"
AGY_BACKEND = agy_lane_gates.AGY_LANE_BACKEND
_ALIASES = ("haiku", "sonnet", "opus")
_VENDOR = re.compile(r"^(?:anthropic|claude-cli|claude-kit|agy-cli-lane|codex-cli)/")


class LanesBlockError(ValueError):
    """The profile's `lanes:` block is malformed."""


# --- ids and refs ----------------------------------------------------------------------------------------

def _compact(model_id: str) -> str:
    """`claude-sonnet-5-5` -> `sonnet55`; any other id: lowercase alphanumerics."""
    m = re.match(r"^claude-([a-z]+)-(\d+)(?:-(\d+))?$", model_id)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3) or ''}"
    return re.sub(r"[^a-z0-9]+", "", model_id.lower())


def agent_id_for(backend: str, model: str) -> str:
    """`kit-lane-cli-sonnet55` for claude-cli, `kit-lane-agy-<model>` for an agy backend."""
    bare = _VENDOR.sub("", model)
    kind = "cli" if backend == CLAUDE_CLI else "codex" if backend == CODEX_BACKEND else "agy"
    return f"{AGENT_PREFIX}{kind}-{_compact(bare)}"


def _lane_prefix(backend: str) -> str:
    return CODEX_BACKEND if backend == CODEX_BACKEND else AGY_BACKEND


def _strip(ref: str) -> str:
    return _VENDOR.sub("", str(ref))


# --- profile block -> state fragment ---------------------------------------------------------------------

def validate_block(block: Any) -> list[str]:
    """Problems with a profile `lanes:` block ([] when it validates). It names roles and candidate lists
    only: `claude-code`, `claude-cli:<alias|id>` or `agy:<model>`; never an agent id."""
    if block is None:
        return []
    if not isinstance(block, Mapping):
        return ["lanes: must be a mapping"]
    problems: list[str] = []
    for key in block:
        if key not in ("roles", "second", "mcp", "pins"):
            problems.append(f"lanes.{key}: unknown key (expected roles, second, mcp, pins)")
    for key in ("roles", "second"):
        section = block.get(key) or {}
        if not isinstance(section, Mapping):
            problems.append(f"lanes.{key}: must be a mapping of role -> candidate list")
            continue
        for role, cands in section.items():
            if not roles_policy.lane_eligible(role):
                problems.append(f"lanes.{key}.{role}: role is not lane-eligible")
            if key == "second" and role not in roles_policy.REPORT_ROLES:
                problems.append(f"lanes.second.{role}: only report-contract roles take a second member")
            elif key == "second" and role in roles_policy.CHECKER_ROLES:
                problems.append(f"lanes.second.{role}: a checker role never takes a second member "
                                "(its report directory cannot attribute a write before verify)")
            if not isinstance(cands, list) or not cands:
                problems.append(f"lanes.{key}.{role}: must be a non-empty list")
                continue
            for c in cands:
                if not isinstance(c, str) or not _candidate(c):
                    problems.append(f"lanes.{key}.{role}: bad candidate {c!r} "
                                    "(expected claude-code, claude-cli:<model>, codex:<model> or agy:<model>)")
                elif c.startswith("codex:") and (role not in roles_policy.REPORT_ROLES):
                    problems.append(f"lanes.{key}.{role}: the Codex lane is read-only; role {role} "
                                    "is not a report-contract role")
                elif c.startswith("agy:") and c[4:] not in model_pins.AGY_LANE_MODELS:
                    problems.append(f"lanes.{key}.{role}: agy slug {c[4:]!r} is not in AGY_LANE_MODELS "
                                    f"({', '.join(model_pins.AGY_LANE_MODELS)})")
                elif c.startswith(AGENT_PREFIX) or c.startswith("openclaw-agent:"):
                    problems.append(f"lanes.{key}.{role}: a profile never names an agent id ({c!r})")
    pins = block.get("pins")
    if pins is not None:
        if not isinstance(pins, Mapping):
            problems.append("lanes.pins: must be a mapping of role -> candidate list")
        else:
            for role, cands in pins.items():
                listed = set((block.get("roles") or {}).get(role) or []) | set((block.get("second") or {}).get(role) or [])
                if role in roles_policy.CHECKER_ROLES:
                    problems.append(f"lanes.pins.{role}: a checker takes no operator pin")
                if not isinstance(cands, list) or not cands:
                    problems.append(f"lanes.pins.{role}: must be a non-empty list")
                    continue
                for c in cands:
                    if c not in listed:
                        problems.append(f"lanes.pins.{role}: {c!r} is not one of the role's candidates")
    mcp = block.get("mcp")
    if mcp is not None:
        if not isinstance(mcp, Mapping):
            problems.append("lanes.mcp: must be a mapping of role -> list of MCP server names")
        else:
            known = set(block.get("roles") or {}) | set(block.get("second") or {})
            for role, names in mcp.items():
                if role not in known:
                    problems.append(f"lanes.mcp.{role}: role is not named under lanes.roles or lanes.second")
                if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
                    problems.append(f"lanes.mcp.{role}: must be a list of MCP server names")
    return problems


def _candidate(text: str) -> tuple[str, str] | None:
    if text == CLAUDE_CODE:
        return CLAUDE_CODE, ""
    m = re.match(r"^(claude-cli|codex|agy):([A-Za-z0-9._/-]+)$", text)
    if m:
        kind = {"claude-cli": CLAUDE_CLI, "codex": CODEX_BACKEND}.get(m.group(1), AGY_BACKEND)
        return kind, m.group(2)
    return None


def valid_acks(acks: Mapping[str, Any] | None) -> dict[str, Any]:
    """The acknowledgements that still hold: the stored text hash equals the current text (a text change means
    re-signing) and a Google ack names its account. Everything else is dropped before any resolver sees it (N8)."""
    out: dict[str, Any] = {}
    for provider, ack in (acks or {}).items():
        if not isinstance(ack, Mapping) or provider not in ACK_TEXT:
            continue
        if ack.get("text_sha256") != hashlib.sha256(ACK_TEXT[provider].encode("utf-8")).hexdigest():
            continue
        if provider == "google" and not str(ack.get("account") or "").strip():
            continue
        out[provider] = dict(ack)
    return out


def agy_role_reason(role: str, gates: Mapping[str, Any] | None, acks: Mapping[str, Any] | None) -> str | None:
    """Why an agy candidate of `role` is held, or None when it is admitted. Every condition fails closed, in
    this order: ADR section 11 Accepted, the flag, a valid Google ack, the role listed, gates G0..G7 proven."""
    if not AGY_LANE_ADR_ACCEPTED:
        return HELD_AGY_REASON
    if not bool((gates or {}).get("antigravity")):
        return HELD_FLAG_REASON
    google = valid_acks(acks).get("google")
    if google is None:
        return HELD_NO_ACK_REASON
    if role not in (google.get("roles") or ()):
        return HELD_ROLE_REASON.format(role=role)
    return agy_lane_gates.refusal()


def backend_role_reason(backend: str, role: str, gates: Mapping[str, Any] | None,
                        acks: Mapping[str, Any] | None) -> str | None:
    """Why a non-claude-cli candidate is held. The Codex lane stays held until its ADR is accepted (FU-2);
    every other backend takes the agy admission."""
    if backend == CLAUDE_CLI:
        return None
    if backend == CODEX_BACKEND:
        return held_reason(backend) if backend_held(backend) else None
    return agy_role_reason(role, gates, acks)


def agy_admission(lanes: Mapping[str, Any], gates: Mapping[str, Any] | None,
                  acks: Mapping[str, Any] | None) -> dict[str, str]:
    """Agent id -> reason, for every non-claude-cli agent of a state `lanes` table that is not admitted.

    An agent is admitted when at least one (role, candidate) naming it is admitted. Single source for state,
    the render path and the OpenClaw writer: a state written by an older kit, or one whose ack went stale after
    a kit upgrade, is held here again.
    """
    refs = lane_refs(lanes)
    out: dict[str, str] = {}
    for aid, agent in sorted((lanes.get("agents") or {}).items()):
        if (agent or {}).get("backend") == CLAUDE_CLI:
            continue
        ref = f"openclaw-agent:{aid}"
        roles = sorted(r for r, rs in refs.items() if ref in rs) or [""]
        reasons = [backend_role_reason((agent or {}).get("backend") or "", r, gates, acks) for r in roles]
        if all(reasons):
            out[aid] = str(reasons[0])
    return out


def _agent_id_of(text: str, ids: Mapping[str, str]) -> str | None:
    """The kit agent id a profile candidate text maps to (None for claude-code or an invalid text)."""
    parsed = _candidate(text)
    if parsed is None or parsed[0] == CLAUDE_CODE:
        return None
    backend, model = parsed
    if backend == CLAUDE_CLI:
        bare = ids.get(model, model) if model in _ALIASES else model
        return agent_id_for(backend, model_pins.openclaw_ref(bare))
    return agent_id_for(backend, f"{_lane_prefix(backend)}/{model}")


def state_from_block(block: Mapping[str, Any] | None, *, claude_ids: Mapping[str, str] | None = None,
                     gates: Mapping[str, Any] | None = None,
                     acks: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The `ProfileState.lanes` fragment (`agents`, `roles`, `second`) a profile block proposes.

    `claude_ids` maps an alias to the effective Claude id (default `model_pins.effective()`). An agy candidate
    enters state only when `agy_role_reason` admits it; otherwise it is recorded in `held` with the reason.
    """
    problems = validate_block(block)
    if problems:
        raise LanesBlockError("; ".join(problems))
    ids = dict(claude_ids or model_pins.effective())
    agents: dict[str, Any] = {}
    held: list[dict[str, str]] = []
    current_role = [""]

    def lane_ref(text: str) -> str | None:
        backend, model = _candidate(text)  # type: ignore[misc]
        if backend == CLAUDE_CODE:
            return CLAUDE_CODE
        why = backend_role_reason(backend, current_role[0], gates, acks)
        if why:
            held.append({"role": current_role[0], "candidate": text, "reason": why})
            return None
        if backend == CLAUDE_CLI:
            bare = ids.get(model, model) if model in _ALIASES else model
            ref = model_pins.openclaw_ref(bare)
        else:
            ref = f"{_lane_prefix(backend)}/{model}"
        aid = agent_id_for(backend, ref)
        agents.setdefault(aid, {"backend": backend, "model": ref,
                                "pool": scoring_lanes.pool_of_agent(scoring_lanes.LaneAgent(aid, backend, ref))})
        return f"openclaw-agent:{aid}"

    def refs_of(role: str, cands: list[str]) -> list[str]:
        current_role[0] = role
        return [r for r in (lane_ref(c) for c in cands) if r is not None]

    roles = {r: {"candidates": refs_of(r, cands)}
             for r, cands in sorted(((block or {}).get("roles") or {}).items())}
    second = {r: refs_of(r, cands) for r, cands in sorted(((block or {}).get("second") or {}).items())}
    second = {r: c for r, c in second.items() if c}
    out: dict[str, Any] = {"agents": dict(sorted(agents.items())), "roles": roles, "second": second}
    if held:
        out["held"] = held
    pins = {r: list(c) for r, c in sorted(((block or {}).get("pins") or {}).items()) if c}
    if pins:
        out["pins"] = pins
    # Explicit per-role opt-in for extra MCP servers (ADR-0007 OQ-5). Lane agents are MCP-scoped by default;
    # an agent serving several roles gets the union of their opt-ins.
    mcp = {r: sorted({str(n) for n in names}) for r, names in sorted(((block or {}).get("mcp") or {}).items())}
    if mcp:
        for role, names in mcp.items():
            refs = list((roles.get(role) or {}).get("candidates") or []) + list(second.get(role) or [])
            for ref in refs:
                aid = ref.split(":", 1)[1] if ref.startswith("openclaw-agent:") else None
                if aid in agents:
                    agents[aid]["mcp_servers"] = sorted(set(agents[aid].get("mcp_servers") or []) | set(names))
        out["mcp"] = mcp
    return out


# --- drift -----------------------------------------------------------------------------------------------

def _model_of(by_role: Mapping[str, Any], role: str) -> str:
    cfg = by_role.get(role)
    return str(cfg.get("model") or "") if isinstance(cfg, Mapping) else ""


def _differs(role_model: str, agent_model: str) -> bool:
    """By family for an alias role, by exact ref (vendor prefix ignored) otherwise."""
    if role_model in _ALIASES:
        return roles_policy.model_family(role_model) != roles_policy.model_family(agent_model)
    return _strip(role_model) != _strip(agent_model)


def lane_refs(lanes: Mapping[str, Any]) -> dict[str, list[str]]:
    """role -> every lane ref the state names for it (candidates and second members)."""
    out: dict[str, list[str]] = {}
    for role, cfg in (lanes.get("roles") or {}).items():
        out.setdefault(role, []).extend((cfg or {}).get("candidates") or [])
    for role, cands in (lanes.get("second") or {}).items():
        out.setdefault(role, []).extend(cands or [])
    return out


def explain_lanes(lanes: Mapping[str, Any] | None) -> dict[str, str] | None:
    """role -> text for the explain Lane column, from the state's table; None without a table (the output
    is then byte-identical to the one printed before lanes existed)."""
    if not isinstance(lanes, Mapping) or not (lanes.get("roles") or lanes.get("second")):
        return None
    out: dict[str, str] = {}
    for role, cfg in sorted((lanes.get("roles") or {}).items()):
        out[role] = " > ".join((cfg or {}).get("candidates") or []) or "-"
    for role, cands in sorted((lanes.get("second") or {}).items()):
        out[role] = f"{out.get(role, '-')} + second {' > '.join(cands or [])}"
    for role, names in sorted((lanes.get("mcp") or {}).items()):
        if names:
            out[role] = f"{out.get(role, '-')} [mcp: {', '.join(names)}]"
    for h in lanes.get("held") or []:
        role = h.get("role", "")
        out[role] = f"{out.get(role, '-')} [{h.get('candidate')} {h.get('reason')}]"
    return out


def drift(lanes: Mapping[str, Any] | None, by_role: Mapping[str, Any]) -> list[dict[str, str]]:
    """The lane agents whose model differs from the model of a role that names them."""
    found: dict[str, dict[str, str]] = {}
    if not isinstance(lanes, Mapping):
        return []
    agents = lanes.get("agents") or {}
    ids = model_pins.effective()
    pinned: dict[str, set[str]] = {}
    for r, texts in (lanes.get("pins") or {}).items():
        pinned[r] = {f"openclaw-agent:{aid}" for aid in (_agent_id_of(str(x), ids) for x in texts or ()) if aid}
    for role, refs in sorted(lane_refs(lanes).items()):
        role_model = _model_of(by_role, role)
        if not role_model:
            continue
        for ref in refs:
            if not isinstance(ref, str) or not ref.startswith("openclaw-agent:"):
                continue
            if ref in pinned.get(role, set()):
                continue
            aid = ref.split(":", 1)[1]
            agent = agents.get(aid)
            if not isinstance(agent, Mapping) or aid in found:
                continue
            if _differs(role_model, str(agent.get("model", ""))):
                found[aid] = {"agent": aid, "agent_model": str(agent.get("model", "")),
                              "role": role, "role_model": role_model}
    return [found[k] for k in sorted(found)]


def version_drift(lanes: Mapping[str, Any] | None, by_role: Mapping[str, Any],
                  ids: Mapping[str, str] | None = None) -> list[dict[str, str]]:
    """Lane agents that are now tier 2 for a role they name: the same family, another version (ADR-0007
    section 11 amendment). They are not stale; they are used only as a writer fallback, never for a checker."""
    if not isinstance(lanes, Mapping):
        return []
    eff = dict(ids or model_pins.effective())
    agents = lanes.get("agents") or {}
    found: dict[str, dict[str, str]] = {}
    for role, refs in sorted(lane_refs(lanes).items()):
        role_model = _model_of(by_role, role)
        if not role_model:
            continue
        for ref in refs:
            aid = ref.split(":", 1)[1] if isinstance(ref, str) and ref.startswith("openclaw-agent:") else None
            agent = agents.get(aid) if aid else None
            if not isinstance(agent, Mapping) or aid in found:
                continue
            model = str(agent.get("model", ""))
            if scoring_lanes.version_tier(role_model, model, eff) == 2:
                found[aid] = {"agent": aid, "agent_model": scoring_lanes._bare(model), "role": role,
                              "role_model": scoring_lanes._bare(eff.get(role_model, role_model))}
    return [found[k] for k in sorted(found)]


def drift_warnings(lanes: Mapping[str, Any] | None, by_role: Mapping[str, Any]) -> list[str]:
    out = [f"lane agent {d['agent']} model {d['agent_model']} differs from role model {d['role_model']} "
           f"(role {d['role']}); re-run `ai-resources setup --lanes`" for d in drift(lanes, by_role)]
    out += [f"lane agent {d['agent']} runs {d['agent_model']}; role model is {d['role_model']} (role {d['role']}); "
            f"it is used only as a writer fallback and never for a checker"
            for d in version_drift(lanes, by_role)]
    return out


# --- the rendered document -------------------------------------------------------------------------------

def render_doc(lanes: Mapping[str, Any], by_role: Mapping[str, Any], mode: str,
               acks: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The `lanes.yaml` document: `{mode, agents, roles, second, digest}`.

    Stale agents (model drift) are marked `stale: true` and the digest covers every agent model ref.
    The table itself is resolved by `scoring.lanes.resolve_lanes` at dispatch time; this file carries
    the declared candidates in order, which is what dispatch reads.
    """
    stale = {d["agent"] for d in drift(lanes, by_role)}
    agents: dict[str, Any] = {}
    held = set(agy_admission(lanes, lanes.get("gates"), acks))
    every: dict[str, Any] = {}
    for aid, agent in sorted((lanes.get("agents") or {}).items()):
        entry = {"backend": agent.get("backend", ""), "model": agent.get("model", ""),
                 "pool": agent.get("pool", "")}
        if aid in stale or agent.get("stale"):
            entry["stale"] = True
        every[aid] = entry
        if aid not in held:                # a held agy agent from a hand-edited state never reaches lanes.yaml
            agents[aid] = entry
    def _live(refs: Any) -> list[str]:
        return [r for r in (refs or []) if not (isinstance(r, str) and r.startswith("openclaw-agent:")
                                                and r.split(":", 1)[1] in held)]

    roles = {r: {"candidates": _live((c or {}).get("candidates"))}
             for r, c in sorted((lanes.get("roles") or {}).items())}
    second = {r: _live(c) for r, c in sorted((lanes.get("second") or {}).items())}
    second = {r: c for r, c in second.items() if c}
    payload: dict[str, Any] = {"mode": mode, "agents": agents, "roles": roles, "second": second}
    # Gate and Google-ack fields appear only when an agy gate or a Google ack exists: a phase 2 install's
    # lanes.yaml stays byte-identical.
    google = (acks or {}).get("google")
    if (lanes.get("gates") or {}).get("antigravity") or isinstance(google, Mapping):
        payload["gates"] = {"antigravity": bool((lanes.get("gates") or {}).get("antigravity"))}
    if isinstance(google, Mapping):
        payload["acks"] = {"google": {"roles": sorted(google.get("roles") or ()),
                                      "text_sha256": str(google.get("text_sha256") or ""),
                                      "account": str(google.get("account") or "")}}
    payload["resolved"] = resolve_table(lanes, by_role, mode, acks, every)
    return {**payload, "digest": doc_digest(payload)}


def doc_digest(payload: Mapping[str, Any]) -> str:
    """The digest of a `lanes.yaml` document: every key except `digest` itself (`lanes prepare` recomputes it)."""
    body = {k: v for k, v in payload.items() if k != "digest"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _meta_of(lanes: Mapping[str, Any], role: str) -> dict[str, Any]:
    meta = (lanes.get("role_meta") or {}).get(role) if isinstance(lanes.get("role_meta"), Mapping) else None
    return {k: v for k, v in meta.items() if k in ("evidence", "effort")} if isinstance(meta, Mapping) else {}


def resolve_table(lanes: Mapping[str, Any], by_role: Mapping[str, Any], mode: str,
                  acks: Mapping[str, Any] | None, agents: Mapping[str, Any]) -> dict[str, Any]:
    """The resolver's choice per role, computed at setup / re-render time (ADR-0007 §1, OQ-4).

    `roles.<role>`: `{lane, model, reason, fallbacks: [{lane, reason}]}`; `second.<role>`: `{lane, model,
    reason}`; `blocked.<role>`: the reason a checker has no admissible lane. `fallbacks` are the other
    candidates the resolver admits on their own, in declared order, plus `claude-code` for non-checker roles
    (the off switch): the only lanes `lanes prepare --lane` accepts besides the resolved one. Agy agents are
    held while the Antigravity section is not accepted. A resolver error is recorded, never raised.
    """
    gates_in = lanes.get("gates") or {}
    held_reasons = agy_admission({**dict(lanes), "agents": dict(agents)}, gates_in, acks)
    held_ids = set(held_reasons)
    usable = {aid: dict(a) for aid, a in agents.items() if aid not in held_ids}

    def keep(refs: list[str]) -> list[str]:
        return [r for r in refs if not (r.startswith("openclaw-agent:") and r.split(":", 1)[1] not in usable)]

    candidates = {r: keep(list((c or {}).get("candidates") or [])) for r, c in (lanes.get("roles") or {}).items()}
    second = {r: keep(list(c or [])) for r, c in (lanes.get("second") or {}).items()}
    second = {r: c for r, c in second.items() if c}
    caps = dict(lanes.get("pool_caps") or {})
    # A measured headroom snapshot taken at setup (`propose(headroom=...)`); absent means static weights.
    snap = lanes.get("headroom_weights") or {}
    weights = {str(k): float(v) for k, v in snap.items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)} if isinstance(snap, Mapping) else {}
    ids = model_pins.effective()
    pins: dict[str, list[str]] = {}
    for role, texts in (lanes.get("pins") or {}).items():
        refs = [f"openclaw-agent:{aid}" for aid in (_agent_id_of(str(x), ids) for x in texts or ()) if aid]
        keep_refs = set(candidates.get(role, ())) | set(second.get(role, ()))
        pins[role] = [r for r in refs if r in keep_refs and role not in roles_policy.CHECKER_ROLES]
    gates = {"antigravity_flag": bool(gates_in.get("antigravity")), "adr_0007_agy_accepted": AGY_LANE_ADR_ACCEPTED}
    kw = dict(acks=valid_acks(acks), pool_caps=caps, mode=mode, weights=weights, gates=gates,
              effective_ids=ids, pins={r: v for r, v in pins.items() if v})
    try:
        table = scoring_lanes.resolve_lanes(by_role, usable, candidates, second, **kw)
        out: dict[str, Any] = {"roles": {}, "second": {}, "blocked": dict(sorted(table.blocked.items()))}
        for role in sorted(candidates):
            lane = table.roles.get(role)
            if lane is None:
                continue
            why = "; ".join(w.text for w in table.why if w.role == role and not w.lane.startswith("second:"))
            fallbacks: list[dict[str, str]] = []
            for ref in candidates[role]:
                if ref == lane.ref:
                    continue
                one_pins = {role: [p for p in pins.get(role, ()) if p == ref]} if ref in pins.get(role, ()) else {}
                one = scoring_lanes.resolve_lanes(by_role, usable, {role: [ref]}, {}, **{**kw, "pins": one_pins})
                if role in one.blocked or one.roles.get(role) is None or one.roles[role].ref != ref:
                    continue
                fallbacks.append({"lane": ref, "model": one.models.get(role, ""),
                                  "reason": "admitted candidate, declared order: use only when "
                                            "the resolved lane is unavailable or capped"})
            if role not in roles_policy.CHECKER_ROLES and lane.ref != CLAUDE_CODE and not any(
                    f["lane"] == CLAUDE_CODE for f in fallbacks):
                fallbacks.append({"lane": CLAUDE_CODE, "model": str((by_role.get(role) or {}).get("model") or ""),
                                  "reason": "the off switch"})
            out["roles"][role] = {"lane": lane.ref, "model": table.models.get(role, ""),
                                  "reason": why or "single candidate", "fallbacks": fallbacks,
                                  **_meta_of(lanes, role)}
        for role, lane in sorted(table.second.items()):
            why = "; ".join(w.text for w in table.why if w.role == role and w.lane.startswith("second:"))
            sec_agent = usable.get(lane.ref.split(":", 1)[1]) if lane.ref != CLAUDE_CODE else None
            sec_model = (sec_agent or {}).get("model") or str((by_role.get(role) or {}).get("model") or "")
            out["second"][role] = {"lane": lane.ref, "model": str(sec_model), "reason": why or "second member",
                                   **_meta_of(lanes, role)}
    except (scoring_lanes.LaneError, scoring_lanes.LaneConflict) as exc:
        return {"error": str(exc), "roles": {}, "second": {}, "blocked": {}}
    if held_reasons:
        out["held"] = dict(sorted(held_reasons.items()))
    return out


def dumps(doc: Mapping[str, Any]) -> str:
    if yaml is None:
        raise RuntimeError("PyYAML required to write lanes.yaml")
    return ("# Rendered by `ai-resources setup` (ADR-0007). Do not edit: setup re-renders it.\n"
            + yaml.safe_dump(dict(doc), default_flow_style=False, sort_keys=False))


# --- the writer (backup before write, restore from its own backup) ---------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def current_by_role(s: "state.SetupState") -> dict[str, Any]:
    """The role -> model map the state resolves to now, never read from `lanes.yaml` and never from
    `openclaw.json`. Multi-model reads `executors.yaml` when it exists."""
    from . import profiles
    if s.mode == "multi-model":
        path = state.executors_path()
        if path.is_file() and yaml is not None:
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if isinstance(doc, dict):
                    return dict(doc.get("by_role") or {})
            except (OSError, yaml.YAMLError):
                pass
        base = profiles.load_profile(profiles.saved_or_recommended(s.profile.name, s.backend), "multi-model")
        if s.profile.customized and s.profile.customizations:
            base = profiles.merge_customizations(
                base, {k: v for k, v in s.profile.customizations.items() if k in profiles.KNOWN_ROLES})
        return dict(profiles.to_executors(base, s.backend).get("by_role") or {})
    from .wizard import _single_model_executors
    return dict(_single_model_executors(s).get("by_role") or {})


def expected_text(s: "state.SetupState", by_role: Mapping[str, Any] | None = None) -> str | None:
    """The `lanes.yaml` text for this state, or None when there are no lanes."""
    if not state.lanes_enabled(s):
        return None
    by_role = current_by_role(s) if by_role is None else by_role
    return dumps(render_doc(s.profile.lanes, by_role, s.mode, s.profile.lane_acks))


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


def sync_file(s: "state.SetupState", *, by_role: Mapping[str, Any] | None = None,
              backup_dir: Path | None = None) -> dict[str, Any]:
    """Make `lanes.yaml` match the state, tracking it in `tracking.config_files_written`.

    Lanes non-empty -> render (backed up first); lanes empty -> remove the file and drop the tracking
    entry. Returns `{"action": written|removed|unchanged, "payload": <restore payload or None>}`.
    Never reads or writes `openclaw.json`.
    """
    path = state.lanes_path()
    text = expected_text(s, by_role)
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

ACK_TEXT = {
    "openai": (
        "The Codex lane runs Codex CLI children through your ChatGPT/Codex subscription login via OpenClaw. "
        "Non-interactive use may be billed or limited differently from interactive use. A lane on this "
        "account is admitted only for the read-only roles listed below. The lane is held in this version."),
    "anthropic": (
        "Subscription lanes run Claude Code CLI children through your Claude subscription login via "
        "OpenClaw. Sharing one login between the orchestrator and lane children is a grey zone of the "
        "Anthropic terms, and non-interactive (-p / SDK) use may be billed differently from interactive use. "
        "A lane on this account is admitted only for the roles listed below. The spawn itself is not "
        "gated per role by the kit: an agent admitted for one listed role can be spawned by an orchestrator for "
        "any task, and only `lanes prepare --lane` refuses a role the acknowledgement does not list, so a step "
        "that skipped `prepare` has already sent its repository content to Anthropic."),
    "google": (
        "The Antigravity lane runs `agy -p` children through OpenClaw on the Google account named below. The "
        "OpenClaw docs (providers/google.md) state that the Antigravity terms prohibit third-party tools from "
        "accessing the service through Antigravity OAuth, and this lane does exactly that: you accept the risk "
        "that Google suspends the account, and a suspension covers Antigravity, Gemini CLI and Code Assist and "
        "may reach the account's other Google services. This acknowledgement admits nothing by itself: gates "
        "G0-G7 still decide whether the lane is admitted. Use an account that is not used for "
        "wildbit GCP or other critical work if your subscription allows it. Repository content, diffs, test "
        "output and MCP results of every listed role go to Google and cannot be recalled. An agy child runs with "
        "--dangerously-skip-permissions, a full shell. Without a usable sandbox the tool exclusions bound the "
        "tool surface, not what the child can do: the shell still reaches `openclaw message`, `openclaw cron`, "
        "`crontab`, `systemd-run --user`, `curl` and engram's on-disk store; the kit detects only the cron "
        "store, `crontab -l`, the user timer list and unit files. If the kit's agy home override is the HOME "
        "variable, the child also loses its git identity, `PATH` shims and `~/.ssh`; without an override the "
        "shared `~/.gemini` state feeds your later interactive agy sessions. Placement uses static weights "
        "(claude 1.0, agy 0.7): it balances by role count in sorted role order, not by quota. The spawn itself is "
        "not gated per role by the kit: an agent admitted for one listed role can be spawned for any "
        "task, and only `lanes prepare --lane` refuses a role not listed, so a step that skipped `prepare` has "
        "already sent its repository content to Google. Do not use agy interactively during a lane run. A lane "
        "on this account is admitted only for the roles listed below."),
}


def propose(profile_doc: Mapping[str, Any], by_role: Mapping[str, Any], mode: str, *,
            orchestrators: Any = (), acks: Mapping[str, Any] | None = None,
            claude_ids: Mapping[str, str] | None = None,
            headroom: Mapping[str, float] | None = None,
            gates: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The `ProfileState.lanes` a profile proposes, checked with the pure resolver.

    Raises `LanesBlockError` (no or malformed block), `scoring.lanes.LaneError` or
    `scoring.lanes.LaneConflict` (a checker could run a writer's family: single-model primaries, or the
    multi-model LiteLLM union). Nothing is written.
    """
    block = profile_doc.get("lanes")
    if not block:
        raise LanesBlockError("the profile declares no lanes")
    frag = state_from_block(block, claude_ids=claude_ids, gates=gates, acks=acks)
    candidates = {r: list(c.get("candidates") or []) for r, c in frag["roles"].items()}
    ids = dict(claude_ids or model_pins.effective())
    pins: dict[str, list[str]] = {}
    for role, texts in (frag.get("pins") or {}).items():
        refs = [f"openclaw-agent:{aid}" for aid in (_agent_id_of(str(x), ids) for x in texts) if aid]
        keep_refs = set(candidates.get(role, ())) | set((frag["second"].get(role) or ()))
        pins[role] = [r for r in refs if r in keep_refs]
    flag = bool((gates or {}).get("antigravity"))
    scoring_lanes.resolve_lanes(by_role, frag["agents"], candidates, frag["second"],
                                acks=valid_acks(acks), mode=mode, effective_ids=ids,
                                gates={"antigravity_flag": flag, "adr_0007_agy_accepted": AGY_LANE_ADR_ACCEPTED},
                                pins={r: v for r, v in pins.items() if v})
    out = {**frag, "orchestrators": sorted({str(o) for o in orchestrators}), "verify_capability": {},
           "pool_caps": {}, "max_remote_concurrency": 2, "workspace_root": ""}
    meta = _role_meta(profile_doc, set(frag["roles"]) | set(frag["second"]))
    if meta:
        out["role_meta"] = meta
    if flag:
        out["gates"] = {"antigravity": True}
    if headroom:
        out["headroom_weights"] = {str(k): float(v) for k, v in sorted(headroom.items())}
    return out


def _role_meta(profile_doc: Mapping[str, Any], roles: set[str]) -> dict[str, dict[str, Any]]:
    """Read-only per-role metadata a profile declares: the evidence tag (`evidence:`) and `role_effort:`.

    Where the profile has a `role_effort:` map every lane role gets a value (unlisted roles: `default`). Neither
    ever changes routing; both only travel into `lanes.yaml resolved:` for the operator."""
    ev = profile_doc.get("evidence")
    ev_out: dict[str, str] = {}
    if isinstance(ev, Mapping):
        for k in ("tag", "label", "confidence", "source"):
            if isinstance(ev.get(k), str) and ev.get(k):
                ev_out[k] = str(ev[k])
    effort = profile_doc.get("role_effort")
    effort = effort if isinstance(effort, Mapping) else None
    out: dict[str, dict[str, Any]] = {}
    for role in sorted(roles):
        entry: dict[str, Any] = {}
        if ev_out:
            entry["evidence"] = dict(ev_out)
        if effort is not None:
            entry["effort"] = str(effort.get(role) or "default")
        if entry:
            out[role] = entry
    return out


def lane_roles(lanes: Mapping[str, Any]) -> list[str]:
    """Every role the table names (candidates and second members), sorted."""
    return sorted(lane_refs(lanes))


def make_ack(provider: str, roles: list[str], now: str, *, account: str = "") -> dict[str, Any]:
    """`account` is the Google account the operator names (one acknowledgement per account, OQ-1)."""
    text = ACK_TEXT[provider]
    out = {"date": now, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text,
           "roles": sorted(roles)}
    if provider == "google":
        out["account"] = account.strip()
    return out


def acked(acks: Mapping[str, Any] | None, account: str, roles: list[str]) -> bool:
    ack = valid_acks(acks).get(account)
    return ack is not None and set(roles) <= set(ack.get("roles") or ())
