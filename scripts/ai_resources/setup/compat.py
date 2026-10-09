"""Cockpit x provider x mode compatibility matrix, as code.

One table drives the wizard summary, the apply gate, the generated instruction text,
docs/multi-model.md, rule 017 and the update fan-out. Every cell cites the file and line
that justify it, so a reader can check the table against the code instead of trusting it.

Actions:
    configure          the kit points the cockpit at the model (Gemini CLI: at its auth) directly.
    via_gateway        the kit writes the gateway endpoint; multi-model only.
    instructions_only  the tool picks its own model; the kit writes instructions/MCP and claims
                       nothing about routing.
    skip               the kit cannot configure this model for this cockpit.

A cell with ``verified=False`` behaves as ``skip`` ("not verified yet") unless the caller opts in
with ``allow_unverified``. S1 (the spike) flips a cell to verified only with evidence.

Kit content (skills, hooks, MCP, instruction blocks) is written for every detected cockpit
whatever the action (Addendum A2): the action governs the MODEL setting only.

This module is pure: it reads no file, runs no command and imports no cockpit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .providers import PROVIDERS

CONFIGURE = "configure"
VIA_GATEWAY = "via_gateway"
INSTRUCTIONS_ONLY = "instructions_only"
SKIP = "skip"
ACTIONS = (CONFIGURE, VIA_GATEWAY, INSTRUCTIONS_ONLY, SKIP)

SINGLE = "single-model"
MULTI_LITELLM = "multi-model:litellm"
MULTI_OPENROUTER = "multi-model:openrouter"
MODES = (SINGLE, MULTI_LITELLM, MULTI_OPENROUTER)

# openrouter is a backend, not a model family: it is not a matrix column (plan 3.1).
PROVIDER_COLUMNS: tuple[str, ...] = tuple(p for p in PROVIDERS if p != "openrouter")

# Cockpit ids in matrix order. `continue` is the registry key of the continue_dev module.
COCKPITS: tuple[str, ...] = ("claude", "gemini", "cursor", "codex", "aider", "copilot",
                             "windsurf", "continue", "opencode", "openclaw")
REPORT_ONLY: tuple[str, ...] = ("agy", "claude-desktop")
ENGINES: tuple[str, ...] = ("claude-code", "codex", "antigravity", "native")

UNVERIFIED = "not verified yet"
BEST_FIRST = (CONFIGURE, VIA_GATEWAY, INSTRUCTIONS_ONLY, SKIP)


@dataclass(frozen=True)
class Cell:
    action: str
    source: str
    verified: bool = True
    reason: str = ""
    prerequisite: str = ""
    report_only: bool = False


@dataclass
class CockpitAction:
    cockpit: str
    action: str
    model_refs: list[str] = field(default_factory=list)
    gateway: str = ""
    reason: str = ""
    files: list[str] = field(default_factory=list)
    unverified: bool = False
    kit_content: bool = True
    report_only: bool = False
    notes: list[str] = field(default_factory=list)


def _cell(action: str, source: str, *, verified: bool = True, reason: str = "",
          prerequisite: str = "") -> Cell:
    return Cell(action, source, verified, reason, prerequisite)


# --- cell construction -------------------------------------------------------------------------
_NO_GATEWAY = "runs only Claude models without a gateway"
_CLAUDE_SRC = "claude.py:85, 98-100, 361-364"
_CLAUDE_GW_SRC = "claude.py:318-319, 358-360; litellm.py:117-129"
_CLAUDE_OR_SRC = "claude.py:264-282, 321-352; providers.py:140-158"
_AIDER_SRC = "aider.py:37-51, 79-82; litellm.py:136-202"
_AIDER_OR_SRC = "aider.py:47; profiles.py:106; docs: openrouter.ai/docs/quickstart (/api/v1)"
_GEMINI_SRC = "gemini.py:25-69"
_INSTR_SRC = {
    "codex": "codex.py:23-43",
    "cursor": "cursor.py:24-51",
    "copilot": "copilot.py:23-30",
    "windsurf": "windsurf.py:23-30",
    "continue": "continue_dev.py:23-30",
    "opencode": "opencode.py:23-30",
}
_OPENCLAW_SRC = "openclaw.py:97-133, 296-344"
_AGY_SRC = "detection.py:170-192; cockpits/__init__.py:23-35"
_DESKTOP_SRC = "_shared.py:564"


def _claude(provider: str, mode: str) -> Cell:
    if mode == SINGLE:
        if provider == "anthropic":
            return _cell(CONFIGURE, _CLAUDE_SRC)
        return _cell(SKIP, _CLAUDE_SRC, reason=f"Claude Code {_NO_GATEWAY}")
    if mode == MULTI_LITELLM:
        if provider == "anthropic":
            return _cell(VIA_GATEWAY, _CLAUDE_GW_SRC, prerequisite="LiteLLM gateway")
        return _cell(VIA_GATEWAY, _CLAUDE_GW_SRC, verified=False, reason=UNVERIFIED,
                     prerequisite="LiteLLM gateway with a Claude passthrough")
    # OpenRouter
    if provider == "anthropic":
        return _cell(VIA_GATEWAY, _CLAUDE_OR_SRC, prerequisite="OpenRouter key")
    if provider in ("google", "openai", "deepseek", "moonshot"):
        return _cell(VIA_GATEWAY, _CLAUDE_OR_SRC, verified=False, reason=UNVERIFIED,
                     prerequisite="OpenRouter key")
    return _cell(SKIP, _CLAUDE_OR_SRC, reason=f"OpenRouter does not serve {provider} models")


def _aider(provider: str, mode: str) -> Cell:
    if mode == SINGLE:
        return _cell(SKIP, _AIDER_SRC, reason="Aider needs a gateway (no model setting in single-model)")
    if mode == MULTI_LITELLM:
        return _cell(VIA_GATEWAY, _AIDER_SRC, prerequisite="LiteLLM gateway")
    if provider in ("vertex", "ollama"):
        return _cell(SKIP, _AIDER_OR_SRC, reason=f"OpenRouter does not serve {provider} models")
    return _cell(VIA_GATEWAY, _AIDER_OR_SRC, prerequisite="OpenRouter key")


def _gemini(provider: str, mode: str) -> Cell:
    if provider == "google":
        return _cell(CONFIGURE, _GEMINI_SRC,
                     reason="auth only; the model is chosen in the tool")
    return _cell(INSTRUCTIONS_ONLY, _GEMINI_SRC, reason="the tool keeps its own model settings")


def _instructions(cockpit: str) -> Cell:
    return _cell(INSTRUCTIONS_ONLY, _INSTR_SRC[cockpit], reason="the tool keeps its own model settings")


def _openclaw(provider: str, mode: str) -> Cell:
    """Cockpit-level OpenClaw cell: the best engine that can run this provider's models."""
    if provider == "anthropic":
        return _cell(CONFIGURE, "openclaw.py:116-120", prerequisite="engine claude-code")
    if provider == "openai":
        return _cell(CONFIGURE, "openclaw.py:121-125", prerequisite="engine codex (codex installed)")
    return _cell(SKIP, _OPENCLAW_SRC, verified=False,
                 reason=f"no verified OpenClaw runtime for {provider} models ({UNVERIFIED})")


def _cockpit_cell(cockpit: str, provider: str, mode: str) -> Cell:
    if cockpit == "claude":
        return _claude(provider, mode)
    if cockpit == "aider":
        return _aider(provider, mode)
    if cockpit == "gemini":
        return _gemini(provider, mode)
    if cockpit == "openclaw":
        return _openclaw(provider, mode)
    return _instructions(cockpit)


def _report_cell(cockpit: str) -> Cell:
    if cockpit == "agy":
        return Cell(SKIP, _AGY_SRC, True, "used only through OpenClaw's Antigravity engine",
                    "", report_only=True)
    return Cell(SKIP, _DESKTOP_SRC, True, "not managed by ai-resources", "", report_only=True)


def _engine_cell(engine: str, provider: str) -> Cell:
    """OpenClaw engine cells (any mode)."""
    if engine == "claude-code":
        if provider == "anthropic":
            return _cell(CONFIGURE, "openclaw.py:116-120")
        return _cell(SKIP, "openclaw.py:116-120", reason="claude-code engine runs Claude refs only")
    if engine == "codex":
        if provider == "openai":
            return _cell(CONFIGURE, "openclaw.py:121-125", prerequisite="codex installed")
        return _cell(SKIP, "openclaw.py:121-125", reason="codex engine runs OpenAI refs only")
    if engine == "antigravity":
        if provider in ("anthropic", "google", "openai"):
            return _cell(CONFIGURE, "openclaw.py:97-115, 1019-1026; model_pins.py:33-38",
                         prerequisite="agy installed and the unrestricted-execution risk acknowledged",
                         reason="agy's own model ids (for example gemini-3.8-flash-low), never API ids")
        return _cell(SKIP, "openclaw.py:97-115", reason="agy offers no models of this provider")
    # native provider ref
    if provider == "google":
        return _cell(CONFIGURE, _OPENCLAW_SRC, verified=False, reason=UNVERIFIED)
    return _cell(SKIP, _OPENCLAW_SRC, reason="no native OpenClaw runtime for this provider")


def _build() -> tuple[dict, dict, dict]:
    cells: dict[tuple[str, str, str], Cell] = {}
    for c in COCKPITS:
        for p in PROVIDER_COLUMNS:
            for m in MODES:
                cells[(c, p, m)] = _cockpit_cell(c, p, m)
    for c in REPORT_ONLY:
        for p in PROVIDER_COLUMNS:
            for m in MODES:
                cells[(c, p, m)] = _report_cell(c)
    engines = {(e, p): _engine_cell(e, p) for e in ENGINES for p in PROVIDER_COLUMNS}
    return cells, engines, {}


CELLS, ENGINE_CELLS, _ = _build()


def cell(cockpit: str, provider: str, mode_key: str) -> Cell:
    return CELLS[(cockpit, provider, mode_key)]


def mode_key(mode: str, backend: str | None) -> str:
    """Matrix mode column from the wizard's (mode, backend) pair."""
    if mode == "single-model":
        return SINGLE
    return MULTI_OPENROUTER if backend == "openrouter" else MULTI_LITELLM


# --- selections --------------------------------------------------------------------------------
def selection_refs(selection) -> list[tuple[str, str]]:
    """[(provider, ref)] for every slot of a selection; None means the implicit Claude set.

    A selection exposes ``slots`` (``"<provider>:<family>"`` -> object or mapping with ``ref``).
    """
    if selection is None:
        return [("anthropic", f"anthropic/{f}") for f in ("opus", "sonnet", "haiku", "fable")]
    out = []
    for key, slot in selection.slots.items():
        provider = key.split(":", 1)[0]
        ref = slot.get("ref") if isinstance(slot, dict) else getattr(slot, "ref", "")
        out.append((provider, ref or key))
    return out


def providers_of(selection) -> list[str]:
    seen: list[str] = []
    for p, _ in selection_refs(selection):
        if p not in seen:
            seen.append(p)
    return seen


def needs_backend(selection, detected: Iterable[str], mode: str = "multi-model") -> bool:
    """True when a gateway is needed to serve the picks for at least one detected cockpit.

    Claude Code and Aider are the cockpits whose model setting goes through a gateway; the pick
    needs a backend when a non-Claude provider is chosen and one of them is detected, or when
    Aider is detected at all (it has no single-model setting).
    """
    det = set(detected)
    providers = providers_of(selection)
    non_claude = [p for p in providers if p != "anthropic"]
    if "aider" in det:
        return True
    return bool(non_claude) and "claude" in det


def _combine(actions: list[tuple[str, str, bool, str, str]]) -> tuple[str, str, bool]:
    """Pick the best action across providers. Returns (action, reason, unverified)."""
    for want in BEST_FIRST:
        hits = [a for a in actions if a[0] == want]
        if hits:
            unverified = any(h[2] for h in hits)
            reason = "; ".join(sorted({a[3] for a in actions if a[3] and a[0] == SKIP})) \
                if want == SKIP else ""
            return want, reason, unverified
    return SKIP, "", False


def plan(selection, mode: str, backend: str | None, detected: Iterable[str],
         engine: str | None = None, allow_unverified: bool = False) -> list[CockpitAction]:
    """One CockpitAction per detected cockpit (matrix order), then report-only rows."""
    mk = mode_key(mode, backend)
    det = list(detected)
    refs = selection_refs(selection)
    gateway = "" if mk == SINGLE else ("OpenRouter" if mk == MULTI_OPENROUTER else "LiteLLM")
    out: list[CockpitAction] = []
    for cid in COCKPITS:
        if cid not in det:
            continue
        per_provider = []
        served: list[str] = []
        notes: list[str] = []
        for provider, ref in refs:
            if cid == "openclaw" and engine:
                c = ENGINE_CELLS[(engine, provider)]
            else:
                c = CELLS[(cid, provider, mk)]
            act, reason, unv = c.action, c.reason, False
            if not c.verified:
                if allow_unverified and act != SKIP:
                    unv = True
                else:
                    act, reason = SKIP, c.reason or UNVERIFIED
            per_provider.append((act, c.source, unv, reason, c.prerequisite))
            if act != SKIP:
                served.append(ref)
            elif reason:
                notes.append(f"{ref}: {reason}")
        act, reason, unverified = _combine(per_provider)
        if act == SKIP and not reason:
            reason = "no supported model in the selection"
        out.append(CockpitAction(
            cockpit=cid, action=act,
            model_refs=served if act in (CONFIGURE, VIA_GATEWAY) else [],
            gateway=gateway if act == VIA_GATEWAY else "",
            reason=reason, unverified=unverified,
            notes=[n for n in notes if act != SKIP],
        ))
    for cid in REPORT_ONLY:
        if cid in det:
            c = _report_cell(cid)
            out.append(CockpitAction(cid, SKIP, reason=c.reason, kit_content=False, report_only=True))
    return out


def engines_for(selection, detected: Iterable[str] = (), allow_unverified: bool = False) -> list[str]:
    """OpenClaw engines with a verified cell for every picked provider that has one.

    Antigravity is returned too but is never the default (it needs the risk acknowledgment);
    the caller offers it as a separate, labelled choice.
    """
    det = set(detected)
    providers = providers_of(selection)
    out = []
    for e in ENGINES:
        cs = [ENGINE_CELLS[(e, p)] for p in providers]
        usable = [c for c in cs if c.action != SKIP and (c.verified or allow_unverified)]
        if not usable:
            continue
        if e == "codex" and det and "codex" not in det:
            continue
        if e == "antigravity" and det and "agy" not in det:
            continue
        out.append(e)
    return out


# --- documentation -----------------------------------------------------------------------------
_LABEL = {CONFIGURE: "configure", VIA_GATEWAY: "via gateway", INSTRUCTIONS_ONLY: "instructions only",
          SKIP: "skip"}


def _short(c: Cell) -> str:
    label = _LABEL[c.action]
    return label + " (unverified)" if not c.verified and c.action != SKIP else label


def markdown_table() -> str:
    """The compatibility matrix as Markdown; docs/multi-model.md embeds this byte for byte."""
    head = "| Cockpit | Mode | " + " | ".join(PROVIDER_COLUMNS) + " | Source |"
    sep = "|" + "|".join(["---"] * (len(PROVIDER_COLUMNS) + 3)) + "|"
    lines = [head, sep]
    for cid in COCKPITS:
        for mk in MODES:
            row = [CELLS[(cid, p, mk)] for p in PROVIDER_COLUMNS]
            lines.append(f"| {cid} | {mk} | " + " | ".join(_short(c) for c in row)
                         + f" | {row[0].source} |")
    for cid in REPORT_ONLY:
        c = _report_cell(cid)
        lines.append(f"| {cid} | any | report-only: {c.reason} |"
                     + " |" * (len(PROVIDER_COLUMNS) - 1) + f" {c.source} |")
    lines.append("")
    lines.append("OpenClaw engines (any mode):")
    lines.append("")
    lines.append("| Engine | " + " | ".join(PROVIDER_COLUMNS) + " | Source |")
    lines.append("|" + "|".join(["---"] * (len(PROVIDER_COLUMNS) + 2)) + "|")
    for e in ENGINES:
        row = [ENGINE_CELLS[(e, p)] for p in PROVIDER_COLUMNS]
        src = next((c.source for c in row if c.action != SKIP), row[0].source)
        lines.append(f"| {e} | " + " | ".join(_short(c) for c in row) + f" | {src} |")
    return "\n".join(lines) + "\n"
