"""The Haiku notification route: a cheap agent for the Slack channels that only carry notifications (ADR-0014 D4).

A channel that posts alerts and summaries does not need the host's Sonnet orchestrator. This module can add ONE agent
entry on the pinned Haiku (`agents.entries.kit-notifications`) and `bindings[]` peer routes that send the Slack channels
the operator ticks to it. It is a sibling of `openclaw_slack_cost.py` with its own guard, NOT a widening of that writer or
of `assert_channels_safe`: it never writes `channels.*` and never `tools.*` (ADR-0011).

Rules, all enforced by `assert_notification_route_safe`:
- the only agent it may write is `kit-notifications`, on an `anthropic/...haiku...` primary with no fallbacks;
- the only array it may send is `bindings`, whole and with `--replace-path bindings`; every entry that is not the kit's is
  byte for byte the live one, in the live order, and the kit's routes are `slack` + `channel` peers for `kit-notifications`;
- fill-only: a channel the live array already routes (to any agent) is reported and left alone; an existing
  `kit-notifications` entry the kit did not create is kept as it is and never routed to unless it already is on Haiku;
- one confirm per channel (the checkbox), then a dry run; a key that needs a restart (or the rules cannot classify) is NOT
  applied: it is recorded in `host_restart_pending` and `ai-resources openclaw apply-pending` re-derives it in a drained
  window. Never applied unattended;
- every change is recorded (`openclaw.notification_route_changes`) so teardown removes exactly the kit's entries.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field

AGENT_ID = "kit-notifications"
_CHANNEL_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HAIKU_REF = re.compile(r"^anthropic/[A-Za-z0-9._-]*haiku[A-Za-z0-9._-]*$")
COMMENT = "Slack notifications channel routed to the Haiku agent (ai-resources)"


def route_entry(channel_id: str) -> dict:
    return {"type": "route", "agentId": AGENT_ID, "comment": COMMENT,
            "match": {"channel": "slack", "peer": {"kind": "channel", "id": channel_id}}}


def _is_kit_route(entry) -> bool:
    return (isinstance(entry, dict) and entry.get("agentId") == AGENT_ID and entry.get("type") == "route"
            and isinstance(entry.get("match"), dict) and entry["match"].get("channel") == "slack"
            and isinstance(entry["match"].get("peer"), dict) and entry["match"]["peer"].get("kind") == "channel"
            and bool(_CHANNEL_ID.match(str(entry["match"]["peer"].get("id", "")))))


def _peer_key(entry):
    match = entry.get("match") if isinstance(entry, dict) else None
    peer = match.get("peer") if isinstance(match, dict) else None
    if not isinstance(peer, dict):
        return None
    return (match.get("channel"), peer.get("kind"), str(peer.get("id")))


def assert_notification_route_safe(patch: dict, replace_paths: list[str] | None, live_bindings, *,
                                   teardown: bool = False, recorded: list[dict] | None = None) -> None:
    """Refuse anything but the kit's agent entry and its `bindings` routes. Raises ValueError.

    `live_bindings` is the array as it is now: every non-kit entry of the new array must equal the live one, in order.
    In a teardown the agent entry may be null (deleted) and the kit routes may only disappear, never appear."""
    rp = list(replace_paths or [])
    if rp not in ([], ["bindings"]):
        raise ValueError("the notification route may only send --replace-path bindings; refused: " + ", ".join(rp))
    allowed_top = {"agents", "bindings"}
    extra = set(patch) - allowed_top
    if extra:
        raise ValueError("the notification route writes only agents.entries." + AGENT_ID + " and bindings; refused: "
                         + ", ".join(sorted(extra)))
    agents = patch.get("agents")
    if agents is not None:
        entries = agents.get("entries") if isinstance(agents, dict) and set(agents) == {"entries"} else None
        if not isinstance(entries, dict) or set(entries) != {AGENT_ID}:
            raise ValueError("the notification route may only write agents.entries." + AGENT_ID)
        entry = entries[AGENT_ID]
        if entry is None:
            if not teardown:
                raise ValueError("the notification agent is only deleted by a teardown")
        else:
            model = entry.get("model") if isinstance(entry, dict) and set(entry) == {"model"} else None
            if not isinstance(model, dict) or set(model) - {"primary", "fallbacks"}:
                raise ValueError("the notification agent carries only model.primary and model.fallbacks")
            if not isinstance(model.get("primary"), str) or not _HAIKU_REF.match(model["primary"]):
                raise ValueError("the notification agent runs on the pinned Haiku only")
            if model.get("fallbacks", []) != []:
                raise ValueError("the notification agent has no fallbacks")
    if "bindings" in patch:
        new = patch["bindings"]
        if not isinstance(new, list):
            raise ValueError("bindings must be an array")
        if rp != ["bindings"]:
            raise ValueError("the bindings array is replaced whole (--replace-path bindings)")
        live = live_bindings if isinstance(live_bindings, list) else []
        key = lambda e: json.dumps(e, sort_keys=True)          # noqa: E731
        live_keys = {key(e) for e in live}
        if teardown:
            # Only entries the kit recorded may disappear, and nothing may be added or reordered.
            recorded_keys = {key(e) for e in (recorded or [])}
            if new != [e for e in live if key(e) not in recorded_keys]:
                raise ValueError("a teardown may only remove the routes the kit recorded")
        else:
            if any(isinstance(e, dict) and e.get("agentId") == AGENT_ID and not _is_kit_route(e) for e in new):
                raise ValueError("a binding to the notification agent must be a slack channel route")
            added = [e for e in new if _is_kit_route(e) and key(e) not in live_keys]
            added_ids = {id(e) for e in added}
            if [e for e in new if id(e) not in added_ids] != live:
                raise ValueError("every binding the kit does not own must stay byte for byte and in order")
            taken = {_peer_key(e) for e in live}
            if any(_peer_key(e) in taken for e in added):
                raise ValueError("the channel already has a route; the kit never replaces one")


@dataclass
class Plan:
    patch: dict = field(default_factory=dict)
    replace_paths: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)          # channels that get a new route
    skipped: list[str] = field(default_factory=list)           # human notes: already routed, agent not on Haiku
    agent_created: bool = False
    agent_entry: dict | None = None
    routes: list[dict] = field(default_factory=list)           # the exact entries added


def slack_channel_labels(doc: dict) -> dict[str, str]:
    """channel id -> a label for the checkbox: the comment of a binding for that channel, else the id."""
    labels: dict[str, str] = {}
    chans = ((doc.get("channels") or {}).get("slack") or {}).get("channels")
    for cid in (chans if isinstance(chans, dict) else {}):
        if isinstance(cid, str) and _CHANNEL_ID.match(cid):
            labels[cid] = cid
    for entry in doc.get("bindings") if isinstance(doc.get("bindings"), list) else []:
        key = _peer_key(entry)
        if key and key[0] == "slack" and key[1] == "channel" and key[2] in labels and isinstance(entry.get("comment"), str):
            labels[key[2]] = f"{key[2]} ({entry['comment']})"
    return labels


def build_plan(doc: dict, channels: list[str], haiku_ref: str) -> Plan:
    """The patch for the channels asked for, from the live `doc`. Fill-only; nothing here writes."""
    plan = Plan()
    live = doc.get("bindings") if isinstance(doc.get("bindings"), list) else []
    existing = (((doc.get("agents") or {}).get("entries")) or {}).get(AGENT_ID)
    if existing is not None:
        primary = ((existing.get("model") or {}).get("primary")) if isinstance(existing, dict) else None
        if not (isinstance(primary, str) and _HAIKU_REF.match(primary)):
            plan.skipped.append(f"agent {AGENT_ID} already exists and is not on Haiku: left as it is, no route added")
            return plan
    else:
        plan.agent_created = True
        plan.agent_entry = {"model": {"primary": haiku_ref, "fallbacks": []}}
    taken = {_peer_key(e): e.get("agentId") for e in live if _peer_key(e) is not None}
    new = copy.deepcopy(live)
    first_peerless = next((i for i, e in enumerate(new) if _peer_key(e) is None), len(new))
    adds: list[dict] = []
    for cid in channels:
        if not _CHANNEL_ID.match(cid):
            plan.skipped.append(f"{cid}: not a Slack channel id")
            continue
        key = ("slack", "channel", cid)
        if key in taken:
            plan.skipped.append(f"{cid}: already routed to agent {taken[key]}; change that binding by hand to use "
                                f"{AGENT_ID}: {snippet_for(cid)}")
            continue
        adds.append(route_entry(cid))
        plan.channels.append(cid)
    if not adds and not plan.agent_created:
        return plan
    if not adds:
        plan.agent_created = False                       # an agent with no route is pointless: write nothing
        plan.agent_entry = None
        return plan
    new[first_peerless:first_peerless] = adds            # before the first peer-less entry: a catch-all must stay last
    plan.routes = adds
    if plan.agent_created:
        plan.patch["agents"] = {"entries": {AGENT_ID: plan.agent_entry}}
    plan.patch["bindings"] = new
    plan.replace_paths = ["bindings"]
    assert_notification_route_safe(plan.patch, plan.replace_paths, live)
    return plan


def snippet_for(channel_id: str) -> str:
    return (f"openclaw config set bindings (edit the entry whose match.peer.id is {channel_id}: "
            f"set agentId to {AGENT_ID})")


def change_records(plan: Plan) -> list[dict]:
    """State records for teardown: the exact entries added, and the agent only when the kit created it."""
    out: list[dict] = []
    if plan.agent_created and plan.agent_entry is not None:
        out.append({"kind": "agent", "id": AGENT_ID, "entry": copy.deepcopy(plan.agent_entry)})
    out += [{"kind": "binding", "entry": copy.deepcopy(e)} for e in plan.routes]
    return out


def restore_plan(records: list[dict], doc: dict) -> tuple[dict, list[str], list[str]]:
    """(patch, replace_paths, notes) that remove exactly the recorded kit entries that are still as the kit wrote them.

    A route the operator changed or removed is ignored; the agent entry is deleted only when it is unchanged and no
    binding other than the kit's own routes points at it."""
    live = doc.get("bindings") if isinstance(doc.get("bindings"), list) else []
    recorded_bindings = [r["entry"] for r in records if isinstance(r, dict) and r.get("kind") == "binding"
                         and _is_kit_route(r.get("entry"))]
    keys = {json.dumps(e, sort_keys=True) for e in recorded_bindings}
    keep = [e for e in live if json.dumps(e, sort_keys=True) not in keys]
    patch: dict = {}
    rp: list[str] = []
    notes: list[str] = []
    if len(keep) != len(live):
        patch["bindings"] = keep
        rp = ["bindings"]
    agent_rec = next((r for r in records if isinstance(r, dict) and r.get("kind") == "agent"), None)
    if agent_rec:
        live_agent = (((doc.get("agents") or {}).get("entries")) or {}).get(AGENT_ID)
        still_routed = any(isinstance(e, dict) and e.get("agentId") == AGENT_ID for e in keep)
        if live_agent is None:
            pass
        elif live_agent != agent_rec.get("entry"):
            notes.append(f"agent {AGENT_ID} was changed since the kit wrote it: left as it is")
        elif still_routed:
            notes.append(f"agent {AGENT_ID} still has routes the kit did not write: left as it is")
        else:
            patch["agents"] = {"entries": {AGENT_ID: None}}
    if patch:
        assert_notification_route_safe(patch, rp, live, teardown=True, recorded=recorded_bindings)
    return patch, rp, notes
