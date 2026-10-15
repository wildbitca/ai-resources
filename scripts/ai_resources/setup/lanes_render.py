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

from .. import model_pins, roles_policy
from ..scoring import lanes as scoring_lanes
from . import state

try:
    import yaml
except ImportError:  # pragma: no cover - handled by ui.require_deps()
    yaml = None  # type: ignore

CLAUDE_CODE = scoring_lanes.CLAUDE_CODE
CLAUDE_CLI = scoring_lanes.CLAUDE_CLI
AGENT_PREFIX = "kit-lane-"
# ADR-0007 §11 (Antigravity lane) is Proposed-deferred: until the operator accepts it, every `agy:` candidate
# is held (fail closed). tests/test_lanes_agy_hold.py fails when this is True while §11 is not Accepted.
AGY_LANE_ADR_ACCEPTED = False
HELD_AGY_REASON = "held: Antigravity lane not accepted"
_ALIASES = ("haiku", "sonnet", "opus")
_VENDOR = re.compile(r"^(?:anthropic|claude-cli|claude-kit)/")


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
    kind = "cli" if backend == CLAUDE_CLI else "agy"
    return f"{AGENT_PREFIX}{kind}-{_compact(bare)}"


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
        if key not in ("roles", "second", "mcp"):
            problems.append(f"lanes.{key}: unknown key (expected roles, second, mcp)")
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
            if not isinstance(cands, list) or not cands:
                problems.append(f"lanes.{key}.{role}: must be a non-empty list")
                continue
            for c in cands:
                if not isinstance(c, str) or not _candidate(c):
                    problems.append(f"lanes.{key}.{role}: bad candidate {c!r} "
                                    "(expected claude-code, claude-cli:<model> or agy:<model>)")
                elif c.startswith(AGENT_PREFIX) or c.startswith("openclaw-agent:"):
                    problems.append(f"lanes.{key}.{role}: a profile never names an agent id ({c!r})")
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
    m = re.match(r"^(claude-cli|agy):([A-Za-z0-9._/-]+)$", text)
    if m:
        return ("claude-cli" if m.group(1) == "claude-cli" else "agy-cli"), m.group(2)
    return None


def state_from_block(block: Mapping[str, Any] | None, *, claude_ids: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The `ProfileState.lanes` fragment (`agents`, `roles`, `second`) a profile block proposes.

    `claude_ids` maps an alias to the effective Claude id (default `model_pins.effective()`).
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
        if backend != CLAUDE_CLI and not AGY_LANE_ADR_ACCEPTED:
            held.append({"role": current_role[0], "candidate": text, "reason": HELD_AGY_REASON})
            return None
        if backend == CLAUDE_CLI:
            bare = ids.get(model, model) if model in _ALIASES else model
            ref = model_pins.openclaw_ref(bare)
        else:
            ref = model
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
    for role, refs in sorted(lane_refs(lanes).items()):
        role_model = _model_of(by_role, role)
        if not role_model:
            continue
        for ref in refs:
            if not isinstance(ref, str) or not ref.startswith("openclaw-agent:"):
                continue
            aid = ref.split(":", 1)[1]
            agent = agents.get(aid)
            if not isinstance(agent, Mapping) or aid in found:
                continue
            if _differs(role_model, str(agent.get("model", ""))):
                found[aid] = {"agent": aid, "agent_model": str(agent.get("model", "")),
                              "role": role, "role_model": role_model}
    return [found[k] for k in sorted(found)]


def drift_warnings(lanes: Mapping[str, Any] | None, by_role: Mapping[str, Any]) -> list[str]:
    return [f"lane agent {d['agent']} model {d['agent_model']} differs from role model {d['role_model']} "
            f"(role {d['role']}); re-run `ai-resources setup --lanes`" for d in drift(lanes, by_role)]


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
    for aid, agent in sorted((lanes.get("agents") or {}).items()):
        entry = {"backend": agent.get("backend", ""), "model": agent.get("model", ""),
                 "pool": agent.get("pool", "")}
        if aid in stale or agent.get("stale"):
            entry["stale"] = True
        agents[aid] = entry
    roles = {r: {"candidates": list((c or {}).get("candidates") or [])}
             for r, c in sorted((lanes.get("roles") or {}).items())}
    second = {r: list(c or []) for r, c in sorted((lanes.get("second") or {}).items())}
    payload = {"mode": mode, "agents": agents, "roles": roles, "second": second}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**payload, "digest": digest}


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
        base = profiles.load_profile(profiles.saved_or_recommended(s.profile.name))
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
    "anthropic": (
        "Subscription lanes run Claude Code CLI children through your Claude subscription login via "
        "OpenClaw. Sharing one login between the orchestrator and lane children is a grey zone of the "
        "Anthropic terms, and non-interactive (-p / SDK) use may be billed differently from interactive use. "
        "A lane on this account is admitted only for the roles listed below."),
}


def propose(profile_doc: Mapping[str, Any], by_role: Mapping[str, Any], mode: str, *,
            orchestrators: Any = (), acks: Mapping[str, Any] | None = None,
            claude_ids: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The `ProfileState.lanes` a profile proposes, checked with the pure resolver.

    Raises `LanesBlockError` (no or malformed block), `scoring.lanes.LaneError` or
    `scoring.lanes.LaneConflict` (a checker could run a writer's family: single-model primaries, or the
    multi-model LiteLLM union). Nothing is written.
    """
    block = profile_doc.get("lanes")
    if not block:
        raise LanesBlockError("the profile declares no lanes")
    frag = state_from_block(block, claude_ids=claude_ids)
    candidates = {r: list(c.get("candidates") or []) for r, c in frag["roles"].items()}
    scoring_lanes.resolve_lanes(by_role, frag["agents"], candidates, frag["second"],
                                acks=dict(acks or {}), mode=mode)
    return {**frag, "orchestrators": sorted({str(o) for o in orchestrators}), "verify_capability": {},
            "pool_caps": {}, "max_remote_concurrency": 2, "workspace_root": ""}


def lane_roles(lanes: Mapping[str, Any]) -> list[str]:
    """Every role the table names (candidates and second members), sorted."""
    return sorted(lane_refs(lanes))


def make_ack(account: str, roles: list[str], now: str) -> dict[str, Any]:
    text = ACK_TEXT[account]
    return {"date": now, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text,
            "roles": sorted(roles)}


def acked(acks: Mapping[str, Any] | None, account: str, roles: list[str]) -> bool:
    ack = (acks or {}).get(account)
    if not isinstance(ack, Mapping):
        return False
    return set(roles) <= set(ack.get("roles") or ()) and \
        ack.get("text_sha256") == hashlib.sha256(ACK_TEXT[account].encode("utf-8")).hexdigest()
