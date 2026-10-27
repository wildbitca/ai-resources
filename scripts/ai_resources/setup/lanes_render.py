"""Subscription lanes: the setup-owned state fragment and the `lanes.yaml` rendering (ADR-0007, ADR-0019).

`~/.config/ai-resources/lanes.yaml` is generated purely from `scoring.routing.ROLES` (plus the profile's `routing:`
overrides): the role matrix, the spill rule, a digest and the Google acknowledgement. It carries no agents, candidates
or resolved table. It is written only when lanes are enabled, in both modes, and re-rendered by the fan-out artifact
(`model_rerender.LanesArtifact`). Nothing here opens `openclaw.json`.

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

from .. import agy_lane_gates
from ..scoring import routing as scoring_routing
from . import state

try:
    import yaml
except ImportError:  # pragma: no cover - handled by ui.require_deps()
    yaml = None  # type: ignore

CLAUDE_CODE = "claude-code"
CLAUDE_CLI = "claude-cli"
AGENT_PREFIX = "kit-lane-"
# ADR-0007 §11 (Antigravity lane): while it is not Accepted, every `agy:` candidate is held (fail closed).
# tests/test_lanes_agy_hold.py pins this constant to the exact status line of §11 in both directions.
# Accepted is necessary and not sufficient: the flag, a valid Google acknowledgement listing the role and the
# phase 3 gates G0..G7 (agy_lane_gates) must all hold, each as an enforced check (`agy_role_reason`).
AGY_LANE_ADR_ACCEPTED = True       # ADR-0007 section 11 Accepted by the operator on 2026-10-16
HELD_AGY_REASON = "held: Antigravity lane not accepted"


def backend_held(backend: str) -> bool:
    """True when candidates and agents of `backend` are held (fail closed): every agy backend while ADR-0007 section 11
    is not Accepted."""
    return backend != CLAUDE_CLI and not AGY_LANE_ADR_ACCEPTED


# Balanced credit policy (docs/decisions/0017-balanced-credit-policy.md): when an agy lane's Antigravity quota is exhausted,
# its turn falls back to the paid Gemini API (the Google credit) before the Anthropic API. Keyed by the lane's model id.
GEMINI_FLASH_REF = "google/gemini-3.8-flash"
LANE_API_FALLBACKS: dict[str, tuple[str, ...]] = {
    "gemini-3.8-flash-high": (GEMINI_FLASH_REF, "anthropic/claude-haiku-5-5"),
}


def lane_api_fallbacks(agent_id: str, primary: str) -> list[str]:
    """The API fallback chain for a `kit-lane-agy-*` agent whose primary is `agy-cli-lane/<model>`; [] for any other
    agent or an unknown model. Pure: it names refs only, no credential."""
    if not str(agent_id).startswith(AGENT_PREFIX + "agy-"):
        return []
    return list(LANE_API_FALLBACKS.get(_strip(primary), ()))


# ADR-0018: the operator waived gates G0-G7 and the "off by default" state of the Antigravity lane (2026-10-27) and keeps
# the Google acknowledgement; the recommended run records it for this account.
OPERATOR_WAIVER_ACCOUNT = "bitgandtter@wildbit.dev"
WAIVER_REF = "docs/decisions/0018-routing-matrix-and-pool-balancing.md#operator-waiver"
HELD_FLAG_REASON = "held: --lanes-antigravity is off"
HELD_NO_ACK_REASON = "held: no valid Google acknowledgement (missing, stale text or no account)"
HELD_ROLE_REASON = "held: the Google acknowledgement does not list {role}"
AGY_BACKEND = agy_lane_gates.AGY_LANE_BACKEND
_VENDOR = re.compile(r"^(?:anthropic|claude-cli|claude-kit|agy-cli-lane)/")


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
    """Why a non-claude-cli candidate is held: it takes the agy admission."""
    if backend == CLAUDE_CLI:
        return None
    return agy_role_reason(role, gates, acks)


def lane_refs(lanes: Mapping[str, Any]) -> dict[str, list[str]]:
    """role -> every lane ref an old-shape state names for it (empty for the matrix shape)."""
    return {role: list((cfg or {}).get("candidates") or []) for role, cfg in (lanes.get("roles") or {}).items()}


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


# --- the rendered document -------------------------------------------------------------------------------

def render_doc(lanes: Mapping[str, Any], mode: str, acks: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The `lanes.yaml` document: `{mode, generated_from, routing, acks?, digest}`, a pure function of the matrix.

    `routing` is `scoring.routing.render_section` over the profile's `routing:` overrides (kept in the lanes state);
    the Google acknowledgement (egress consent, ADR-0007 s11) travels under `acks`. Nothing else is derived."""
    payload: dict[str, Any] = {"mode": mode, "generated_from": "scoring.routing"}
    payload["routing"] = scoring_routing.render_section({"routing": lanes.get("routing")} if lanes.get("routing") else None)
    google = valid_acks(acks).get("google")
    if google is not None:
        payload["acks"] = {"google": {"roles": sorted(google.get("roles") or ()),
                                      "text_sha256": str(google.get("text_sha256") or ""),
                                      "account": str(google.get("account") or "")}}
    return {**payload, "digest": doc_digest(payload)}


def doc_digest(payload: Mapping[str, Any]) -> str:
    """The digest of a `lanes.yaml` document: every key except `digest` itself (`lanes prepare` recomputes it)."""
    body = {k: v for k, v in payload.items() if k != "digest"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def dumps(doc: Mapping[str, Any]) -> str:
    if yaml is None:
        raise RuntimeError("PyYAML required to write lanes.yaml")
    return ("# Generated from scoring.routing by `ai-resources setup` (ADR-0019). Do not edit: setup re-renders it.\n"
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


def expected_text(s: "state.SetupState") -> str | None:
    """The `lanes.yaml` text for this state, or None when there are no lanes."""
    if not state.lanes_enabled(s):
        return None
    return dumps(render_doc(s.profile.lanes, s.mode, s.profile.lane_acks))


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

ACK_TEXT = {
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
        "shared `~/.gemini` state feeds your later interactive agy sessions. Placement follows the "
        "routing matrix and live pool usage (a pool at 85% hands its work to the role's fallback), not a role count. "
        "The spawn itself is "
        "not gated per role by the kit: an agent admitted for one listed role can be spawned for any "
        "task, and only `lanes prepare --lane` refuses a role not listed, so a step that skipped `prepare` has "
        "already sent its repository content to Google. Do not use agy interactively during a lane run. A lane "
        "on this account is admitted only for the roles listed below. [acknowledgement text v2]"),
}


def lanes_state(profile_doc: Mapping[str, Any], *, orchestrators: Any = (),
                gates: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The `ProfileState.lanes` fragment a profile proposes: orchestrators, the profile's `routing:` overrides and the
    Antigravity flag. The role table itself is never stored: it is `scoring.routing.ROLES` (+ overrides) every time.

    Raises `LanesBlockError` for a malformed `routing:` block. Nothing is written."""
    out: dict[str, Any] = {"orchestrators": sorted({str(o) for o in orchestrators})}
    if profile_doc.get("routing"):
        try:
            scoring_routing.merged(profile_doc)      # fail early on a malformed override
        except scoring_routing.RoutingError as exc:
            raise LanesBlockError(str(exc)) from exc
        out["routing"] = profile_doc["routing"]
    if (gates or {}).get("antigravity"):
        out["gates"] = {"antigravity": True}
    return out


def lane_roles(profile_doc: Mapping[str, Any] | None = None) -> list[str]:
    """The lane-eligible team roles of the matrix, sorted: the roles an acknowledgement lists."""
    from .. import roles_policy
    return sorted(r.name for r in scoring_routing.merged(profile_doc).values()
                  if r.kind == "team" and roles_policy.lane_eligible(r.name))


def agy_roles(profile_doc: Mapping[str, Any] | None = None) -> list[str]:
    """The roles whose chain (primary or fallback) names an Antigravity cell, sorted."""
    return sorted(r.name for r in scoring_routing.merged(profile_doc).values()
                  if r.kind == "team" and any(c.startswith("agy:") for c in r.chain))


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
