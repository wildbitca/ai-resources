"""What the starting team really runs: provider mix, held and degraded Gemini, in pure functions.

ADR-0010 PD-1. Nothing here prompts, prints or touches the disk: the wizard, the doctor and the tests read the same
functions, so the held, degrade and next-action lines come from one place.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping, NamedTuple, Sequence

from . import detection, profiles


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


# --- the team as the operator sees it ------------------------------------------------------------------------------

ROWS = ("cli", "single", "openrouter", "litellm")
ON, HELD, OFF = "on", "held", "off"
LINE_SINGLE = ("Gemini not active: 'Each on its own' runs Claude Code only "
               "(choose 'As a team' with Antigravity, or OpenRouter)")
LINE_LITELLM = "Gemini not active: LiteLLM is legacy; choose OpenRouter for Gemini"
CAUSE_MISSING = "agy-missing"
NEXT_AGY = "install agy and sign in (run `agy`), then re-run setup"
LINE_NOT_SELECTED = "Gemini not active: Antigravity not selected"


class LaneRow(NamedTuple):
    """One line of the team table: the role (or its Antigravity lane), the real provider, the model and where it runs
    (`lane`, shown as "Runs in"). `billed_to` is a separate column (ADR-0016 D5): who pays for the run, "unknown" when
    nothing says so; `effort` is the role's level where the cockpit has one."""
    role: str
    provider: str
    model: str
    lane: str
    billed_to: str = "unknown"
    effort: str = ""


@dataclass(frozen=True)
class ProviderMix:
    declared: tuple = ()                 # providers the starting team names, held or not
    active: tuple = ()                   # providers that run now
    held: tuple = ()                     # ((role, reason), ...) for every declared member that does not run
    lines: tuple = ()                    # the held / degrade lines, each said once
    next_action: str = ""                # the one thing the operator does next ("" when nothing)
    states: Mapping = field(default_factory=dict)       # row label -> on | held | off


def agy_candidates(doc: Mapping[str, Any]) -> list[tuple[str, str]]:
    """`[(role, cell)]` for every team role whose PRIMARY cell is on Antigravity in the routing matrix (with the
    profile's `routing:` overrides). A role that only falls back to Antigravity is not an Antigravity member here;
    it still runs there when the primary is unavailable."""
    from ..scoring import routing
    return [(r.name, r.primary) for r in routing.merged(doc).values()
            if r.kind == "team" and r.primary and r.primary.startswith("agy:")]


def gemini_members(doc: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [m for m in agy_candidates(doc) if m[1].startswith("agy:gemini-")]


def member_state(role: str, *, ticked: bool) -> tuple[str, str]:
    """`(on | held | off, reason)` for an Antigravity member of `role`: on when agy is installed and signed in."""
    if not ticked:
        return OFF, "Antigravity not selected"
    return (ON, "") if detection.agy_active() else (HELD, CAUSE_MISSING)


def agy_team_roles(doc: Mapping[str, Any]) -> list[str]:
    """What Antigravity would take in this starting team: one entry per member with its model slug. Display only."""
    return [f"{role}: {cand[4:]}" for role, cand in agy_candidates(doc)]


def cell_text(cand: str, state: str) -> str:
    slug = cand[4:]
    provider = profiles.vendor_of(slug)
    return f"{provider} {slug} via Antigravity ({state})"


BILLED_LABEL = {"subscription": "subscription", "api_per_token": "API per token", "google_plan": "Google plan",
                "unknown": "unknown"}
GATEWAY_BILLED = {"openrouter": "API per token (OpenRouter)", "litellm": "API per token (LiteLLM)"}


def role_lane_rows(base_rows: Sequence[tuple[str, str, str]], doc: Mapping[str, Any], *, row: str, ticked: bool,
                   allow_unverified: bool = False,
                   cell_verified: bool = False, claude_billed: str = "unknown",
                   effort: Mapping[str, str] | None = None) -> list[LaneRow]:
    """The team table: a line per role with its real provider, model, where it runs ("Runs in") and who is billed
    ("Billed to", a separate column), and (on the CLI row) a line per Antigravity member. `claude_billed` is Claude Code's LIVE billing (`live_auth.B_*`); a gateway row bills its gateway."""
    gateway = {"openrouter": "OpenRouter", "litellm": "LiteLLM"}.get(row, "")
    out: list[LaneRow] = []
    for role, vendor, model in base_rows:
        lane = gateway or "Claude Code"
        if gateway and vendor == "google" and row == "openrouter" and not (allow_unverified or cell_verified):
            lane = "OpenRouter (held: session model until you opt in)"
        billed = GATEWAY_BILLED[row] if gateway else BILLED_LABEL.get(claude_billed, "unknown")
        out.append(LaneRow(role, vendor, model, lane, billed, (effort or {}).get(role, "")))
    if row == "cli":
        for role, cand in agy_candidates(doc):
            state, _why = member_state(role, ticked=ticked)
            out.append(LaneRow(f"{role} (lane)", profiles.vendor_of(cand[4:]), cand[4:], cell_text(cand, state),
                               BILLED_LABEL["google_plan"], ""))
    return out


class RoleRow(NamedTuple):
    """One line of the Review table "Who runs what" (ADR-0016 D5): a role on ONE cockpit, with separate columns."""
    role: str
    runs_in: str
    billed_to: str
    model: str
    effort: str


def review_role_rows(base_rows: Sequence[tuple[str, str, str]], doc: Mapping[str, Any], *, selected: Sequence[str],
                     claude_billed: str, openclaw_models: Mapping[str, str], openclaw_billed: Mapping[str, str],
                     claude_effort: Mapping[str, str] | None = None,
                     openclaw_effort: Mapping[str, str] | None = None) -> list[RoleRow]:
    """The Review table: for every role, where it runs and who is billed, per selected cockpit. Pure.

    - Claude Code subagent: model from the team, billing from Claude Code's live sign-in.
    - OpenClaw kit-role agent: the model the kit-role agent has (live, else planned), billing of THAT model's provider from
      what OpenClaw reports ("unknown" for a provider it does not report).
    - agy: every Antigravity member; "held" in Runs in while agy is not installed and signed in.
    Nothing is guessed: a missing fact reads "unknown"."""
    out: list[RoleRow] = []
    if "claude" in selected:
        for role, _vendor, model in base_rows:
            out.append(RoleRow(role, "Claude Code subagent", BILLED_LABEL.get(claude_billed, "unknown"), model,
                               (claude_effort or {}).get(role, "default")))
    if "openclaw" in selected:
        for role, _vendor, _model in base_rows:
            ref = openclaw_models.get(role, "")
            provider = ref.split("/", 1)[0] if "/" in ref else ""
            billed = BILLED_LABEL.get(openclaw_billed.get(provider, "unknown"), "unknown") if provider else "unknown"
            out.append(RoleRow(role, "OpenClaw kit-role agent", billed, ref or "unknown",
                               (openclaw_effort or {}).get(role, "default")))
    if "agy" in selected:
        for role, cand in agy_candidates(doc):
            state, _why = member_state(role, ticked=True)
            runs = {ON: "agy", HELD: "agy (held)", OFF: "agy (off)"}[state]
            out.append(RoleRow(f"{role} (lane)", runs, BILLED_LABEL["google_plan"], cand[4:], "-"))
    return out


def provider_mix(doc: Mapping[str, Any], base_rows: Sequence[tuple[str, str, str]], *, row: str, ticked: bool = False,
                 allow_unverified: bool = False, cell_verified: bool = False,
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
            for role, _cand in members:
                state, why = member_state(role, ticked=ticked)
                states[f"{role} (lane)"] = state
                if state == HELD:
                    held.append((role, why))
            if not ticked:
                lines.append(LINE_NOT_SELECTED)
            elif held:
                next_step = NEXT_AGY
                lines.append(f"Gemini skipped: agy not installed or not signed in ({CAUSE_MISSING}). Next: {next_step}")
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
              agy_installed: bool) -> list[tuple[str, str, str]]:
    """The tools of the team: which tool takes which roles in the starting team."""
    agy_on = cli_row and agy_installed and "agy" in members
    agy_role_set = {role for role, _cand in agy_candidates(doc)} if agy_on else set()
    claude_roles = ", ".join(r for r, _p, _m in rows if r not in agy_role_set) or "none"
    out: list[tuple[str, str, str]] = [("Claude Code", claude_roles, "in team")]
    if cli_row and agy_installed:
        if "agy" in members:
            listed = "; ".join(agy_team_roles(doc)) or "no role in this starting team"
            out.append(("Antigravity", listed, "in team"))
        else:
            out.append(("Antigravity", "not ticked on Your tools: no roles", "off"))
    return out
