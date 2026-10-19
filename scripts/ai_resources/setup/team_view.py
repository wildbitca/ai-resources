"""What the starting team really runs: provider mix, held and degraded Gemini, in pure functions.

ADR-0010 PD-1. Nothing here prompts, prints or touches the disk: the wizard, the doctor and the tests read the same
functions, so the held, degrade and next-action lines come from one place.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping, NamedTuple, Sequence

from .. import agy_lane_gates, model_pins, roles_policy
from . import lanes_render, profiles


def _vendor(cfg: Mapping[str, Any]) -> str:
    model = str(cfg.get("model") or "")
    return model.split("/", 1)[0] if "/" in model else str(cfg.get("provider") or "")


def _enabled(providers: Mapping[str, Any] | None) -> set[str]:
    out: set[str] = set()
    for pid, p in (providers or {}).items():
        on = p.get("enabled") if isinstance(p, Mapping) else getattr(p, "enabled", False)
        if on:
            out.add(pid)
    return out


def degrade_unrouted(executors: Mapping[str, Any], providers: Mapping[str, Any] | None) -> tuple[dict, list[str]]:
    """The executors with every `google/*` role that has no enabled Google provider moved onto its declared Claude
    fallback, plus the visible lines. A Google model left in a fallback list is dropped as well, because the LiteLLM
    renderer would register a `gemini/` deployment for it. A role with no Claude fallback is left as it is and named.

    Only meaningful on the legacy LiteLLM row: OpenRouter routes every vendor through one key."""
    out = copy.deepcopy(dict(executors))
    if "google" in _enabled(providers):
        return out, []
    moved: list[str] = []
    stuck: list[str] = []
    for role, cfg in (out.get("by_role") or {}).items():
        if not isinstance(cfg, dict):
            continue
        fallbacks = [str(f) for f in cfg.get("fallbacks") or []]
        if _vendor(cfg) == "google":
            claude = next((f for f in fallbacks if f.startswith("anthropic/")), None)
            if claude is None:
                stuck.append(role)
                continue
            cfg["model"] = claude
            fallbacks = [f for f in fallbacks if f != claude]
            moved.append(role)
        kept = [f for f in fallbacks if not f.startswith("google/")]
        if kept != fallbacks or "fallbacks" in cfg:
            cfg["fallbacks"] = kept
    defaults = out.get("defaults")
    if isinstance(defaults, dict) and defaults.get("fallbacks"):
        defaults["fallbacks"] = [f for f in defaults["fallbacks"] if not str(f).startswith("google/")]
    lines: list[str] = []
    if moved:
        lines.append("Gemini not active: Google is not enabled for this gateway; "
                     + ", ".join(moved) + " run on their Claude fallback")
    if stuck:
        lines.append("Google is not enabled for this gateway and has no Claude fallback for: " + ", ".join(stuck))
    return out, lines


def next_action(*, gates_on: bool, record_status: str, agy_ticked: bool, missing_roles: list[str]) -> str:
    """The single next step for the operator, in the order the lane opens: sign-in and terms, then the probe window and
    its record, then a commit. "" when nothing is left. One source for the team view, doctor and `lanes gates`."""
    if gates_on and not missing_roles:
        return ""
    if record_status == "invalid":
        return "fix or restore the evidence record, then commit it"
    if not gates_on:
        return "run the probe window, then ai-resources lanes gates record"
    if not agy_ticked:
        return "tick Antigravity in ai-resources setup and sign the Google acknowledgement"
    return "extend the Google acknowledgement to: " + ", ".join(missing_roles)


# --- the team as the operator sees it ------------------------------------------------------------------------------

ROWS = ("cli", "single", "openrouter", "litellm")
ON, HELD, OFF = "on", "held", "off"
DISPLAY_ONLY = "display-only"
# Seconds that no workflow step dispatches yet (only the code-reviewer has `lane_member: second` in
# feature-lanes.workflow.yaml): shown, never reported as "on" (ADR-0010 C5).
DISPLAY_ONLY_SECONDS = frozenset({"planner", "software-architect"})
LINE_SINGLE = ("Gemini not active: 'Each on its own' runs Claude Code only "
               "(choose 'As a team' with Antigravity, or OpenRouter)")
LINE_LITELLM = "Gemini not active: LiteLLM is legacy; choose OpenRouter for Gemini"
LINE_NOT_SELECTED = "Gemini not active: Antigravity not selected"


class LaneRow(NamedTuple):
    """One line of the team table: the role (or its lane/second member), the real provider, the model and the lane."""
    role: str
    provider: str
    model: str
    lane: str


@dataclass(frozen=True)
class ProviderMix:
    declared: tuple = ()                 # providers the starting team names, held or not
    active: tuple = ()                   # providers that run now
    held: tuple = ()                     # ((role, reason), ...) for every declared member that does not run
    lines: tuple = ()                    # the held / degrade lines, each said once
    next_action: str = ""                # the one thing the operator does next ("" when nothing)
    states: Mapping = field(default_factory=dict)       # row label -> on | held | off


def agy_members(doc: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """`[(kind, role, candidate)]` for every Antigravity candidate the profile's lanes block names: `kind` is `lane`
    (the role's own candidate list) or `second`. The default writer is never listed (writing stays on Claude Code)."""
    block = doc.get("lanes") or {}
    out: list[tuple[str, str, str]] = []
    for key, kind in (("roles", "lane"), ("second", "second")):
        for role, cands in (block.get(key) or {}).items():
            if role == roles_policy.DEFAULT_INTEGRATING_WRITER:
                continue
            for c in cands or ():
                if isinstance(c, str) and c.startswith("agy:"):
                    out.append((kind, role, c))
    return out


def agy_lanes_on(lanes: Mapping[str, Any] | None) -> bool:
    """True when the saved lanes table holds an Antigravity lane agent (a fresh install never does)."""
    agents = (lanes or {}).get("agents") or {}
    return any((a or {}).get("backend") == agy_lane_gates.AGY_LANE_BACKEND for a in agents.values())


def gemini_members(doc: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    return [m for m in agy_members(doc) if m[2].startswith("agy:gemini-")]


def member_state(role: str, *, ticked: bool, acks: Mapping[str, Any] | None, kind: str = "lane") -> tuple[str, str]:
    """`(on | held | off | display-only, reason)` for an Antigravity member of `role`, by the same admission rule lanes
    use. A second opinion no workflow step dispatches reads `display-only` where it would read `on`."""
    if not ticked:
        return OFF, "Antigravity not selected"
    why = lanes_render.agy_role_reason(role, {"antigravity": True}, acks)
    if why is not None:
        return HELD, why
    if kind == "second" and role in DISPLAY_ONLY_SECONDS:
        return DISPLAY_ONLY, "no workflow step dispatches it yet"
    return ON, ""


def agy_team_roles(doc: Mapping[str, Any]) -> list[str]:
    """What Antigravity would take in this starting team: one entry per member (`second` ones marked), each with its
    model slug, labelled while the slug is unconfirmed. Display only: nothing here reaches the state."""
    out = []
    for kind, role, cand in agy_members(doc):
        slug = cand[4:]
        tag = "" if model_pins.agy_slug_confirmed(slug) else " [unconfirmed slug]"
        out.append(f"{role}{' (second opinion)' if kind == 'second' else ''}: {slug}{tag}")
    return out


def cell_text(cand: str, state: str) -> str:
    slug = cand[4:]
    provider = profiles.vendor_of(slug)
    tag = "" if model_pins.agy_slug_confirmed(slug) else " [unconfirmed slug]"
    return f"{provider} {slug} via Antigravity{tag} ({state})"


def role_lane_rows(base_rows: Sequence[tuple[str, str, str]], doc: Mapping[str, Any], *, row: str, ticked: bool,
                   acks: Mapping[str, Any] | None, allow_unverified: bool = False,
                   cell_verified: bool = False) -> list[LaneRow]:
    """The team table: a line per role with its real provider, model and lane, and (on the CLI row) a line per
    Antigravity member, the seconds of every report role included."""
    gateway = {"openrouter": "OpenRouter", "litellm": "LiteLLM"}.get(row, "")
    out: list[LaneRow] = []
    for role, vendor, model in base_rows:
        lane = gateway or "Claude Code"
        if gateway and vendor == "google" and row == "openrouter" and not (allow_unverified or cell_verified):
            lane = "OpenRouter (held: session model until you opt in)"
        out.append(LaneRow(role, vendor, model, lane))
    if row == "cli":
        for kind, role, cand in agy_members(doc):
            state, _why = member_state(role, ticked=ticked, acks=acks, kind=kind)
            label = f"{role} (second)" if kind == "second" else f"{role} (lane)"
            out.append(LaneRow(label, profiles.vendor_of(cand[4:]), cand[4:], cell_text(cand, state)))
    return out


def _gate_summary() -> str:
    info = agy_lane_gates.summary()
    text = f"Antigravity gates {len(info['proven'])}/{info['total']} proven"
    if info["partial"]:
        text += f" ({', '.join(info['partial'])} partial)"
    return text


def provider_mix(doc: Mapping[str, Any], base_rows: Sequence[tuple[str, str, str]], *, row: str, ticked: bool = False,
                 acks: Mapping[str, Any] | None = None, allow_unverified: bool = False, cell_verified: bool = False,
                 enabled_providers: Sequence[str] = ()) -> ProviderMix:
    """The single source of the held, degrade and next-action lines for a starting team on `row`."""
    vendors = {v for _r, v, _m in base_rows if v and v != "-"}
    declared = set(vendors)
    held: list[tuple[str, str]] = []
    lines: list[str] = []
    states: dict[str, str] = {}
    next_step = ""
    active = set(vendors) - {"google"}
    google_roles = [r for r, v, _m in base_rows if v == "google"]
    if row == "cli":
        members = gemini_members(doc)
        if members:
            declared.add("google")
            record = agy_lane_gates.record_status()
            missing: list[str] = []
            for kind, role, cand in members:
                state, why = member_state(role, ticked=ticked, acks=acks, kind=kind)
                states[f"{role} ({'second' if kind == 'second' else 'lane'})"] = state
                if state == HELD:
                    held.append((role, why))
                    if why.startswith("held: the Google acknowledgement does not list"):
                        missing.append(role)
            if not ticked:
                lines.append(LINE_NOT_SELECTED)
            elif held:
                gates_on = agy_lane_gates.all_proven()
                next_step = next_action(gates_on=gates_on, record_status=record.status, agy_ticked=True,
                                        missing_roles=sorted(set(missing)))
                reason = (_gate_summary() if not gates_on else held[0][1].removeprefix("held: "))
                lines.append(f"Gemini held: {reason}. Next: {next_step}")
            elif ON in states.values():
                active.add("google")
    elif row == "single":
        if google_roles or gemini_members(doc):
            declared.add("google")
        lines.append(LINE_SINGLE)
    elif row == "litellm":
        lines.append(LINE_LITELLM)
    elif row == "openrouter" and google_roles:
        if allow_unverified or cell_verified:
            active.add("google")
        else:
            held.extend((r, "Claude Code x Google not verified") for r in google_roles)
            lines.append("Gemini held on OpenRouter: Claude Code x Google not verified; "
                         + ", ".join(google_roles) + " use the session model")
    return ProviderMix(tuple(sorted(declared)), tuple(sorted(active)), tuple(held), tuple(lines), next_step, states)


def tool_rows(members: Sequence[str], doc: Mapping[str, Any], rows: Sequence[tuple[str, str, str]], *, cli_row: bool,
              agy_installed: bool, codex_selected: bool, codex_held: bool) -> list[tuple[str, str, str]]:
    """The tools of the team: which tool takes which roles in the starting team. Claude Code takes the roles the
    profile gives it; Antigravity shows its roles only when it is ticked; Codex has no roles yet (held, ADR-0007 FU-2),
    so its row is information and never a choice. Other tools have no role profile and are not listed."""
    roles = ", ".join(r for r, _p, _m in rows) or "none"
    out: list[tuple[str, str, str]] = [("Claude Code", roles, "in team")]
    if cli_row and agy_installed:
        if "agy" in members:
            listed = "; ".join(agy_team_roles(doc)) or "no role in this starting team"
            out.append(("Antigravity", f"{listed} - sends repo content to Google; terms check next",
                        "in team, terms check next"))
        else:
            out.append(("Antigravity", "not ticked on Your tools: no roles", "off"))
    if codex_selected and codex_held:
        out.append(("Codex CLI", "no roles yet (held, ADR-0007 FU-2)", "-"))
    return out
