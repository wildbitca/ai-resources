"""Model selection for the setup wizard: what to offer, what the answers mean, what will happen.

The wizard steps are thin callers of this module. Everything that decides is here and pure (the
discovery, the account detection and the prompts are injected), so a scripted-ui test can drive
each of the three shapes without a terminal, a network or a key:

    single            one model
    single-provider   several models from one provider
    multi-provider    models from several providers

Order (Addendum A1): the tools exist first (steps 1-2 detect and offer to install them), then the
accounts (which providers have credentials), then the models (only what a detected cockpit can use),
then a summary per cockpit, then one confirmation, then the write.
"""
from __future__ import annotations

import argparse
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import model_accounts, model_pins, model_providers, selection as sel_mod
from . import compat
from .providers import KNOWN_MODELS

SHAPE_SINGLE = "single"
SHAPE_ONE_PROVIDER = "single-provider"
SHAPE_MANY_PROVIDERS = "multi-provider"

SHAPE_LABELS = {
    SHAPE_SINGLE: "One model",
    SHAPE_ONE_PROVIDER: "Several models from one provider",
    SHAPE_MANY_PROVIDERS: "Models from several providers",
}
APPLY, CHANGE, CANCEL = "Apply", "Change models", "Cancel"
VENDOR_LISTING_PROVIDERS = tuple(p for p, a in model_providers.REGISTRY.items()
                                 if a.vendor_host and model_providers.SOURCE_VENDOR in a.sources
                                 and model_providers.SOURCE_CATALOG not in a.sources) + ("openai",)


class SelectionError(Exception):
    """A contradiction in the answers or the flags; nothing was written."""


@dataclass
class Candidate:
    provider: str
    family: str
    id: str
    channel: str = "stable"
    source: str = "catalog"        # catalog | vendor | offline list

    @property
    def slot(self) -> str:
        return f"{self.provider}:{self.family}"

    @property
    def ref(self) -> str:
        return f"{self.provider}/{self.id}"

    @property
    def label(self) -> str:
        note = "" if self.channel == "stable" else f" ({self.channel}, not pre-checked)"
        src = " [offline list]" if self.source == "offline list" else ""
        return f"{self.id}{note}{src}"


# --- accounts and candidates -------------------------------------------------------------------

def detect_accounts(deps: model_accounts.Deps | None = None) -> dict[str, model_accounts.Account]:
    return model_accounts.detect(deps or model_accounts.default_deps())


def _order(c: Candidate) -> tuple:
    adapter = model_providers.REGISTRY[c.provider]
    ref = adapter.from_any_spelling(c.id)
    return (c.provider, c.family, c.channel != "stable", tuple(-v for v in (ref.version if ref else ())))


def candidates(accounts: dict, discover: Callable[[sel_mod.Selection], "object"] | None,
               vendor_listing: dict[str, bool] | None = None) -> dict[str, list[Candidate]]:
    """provider -> models to offer, newest stable first, previews after. Only credentialed providers.

    `discover(selection)` returns a `models.Discovery` (None: offline). Where it knows nothing about a
    provider, the offline `KNOWN_MODELS` list is used and labelled as such."""
    vendor_listing = vendor_listing or {}
    probe = sel_mod.Selection(shape=SHAPE_MANY_PROVIDERS)
    for pid, adapter in model_providers.REGISTRY.items():
        acc = accounts.get(pid)
        if pid == "openrouter" or acc is None or not acc.credentialed:
            continue
        probe.providers[pid] = sel_mod.ProviderSel(enabled=True, detected_via=list(acc.sources),
                                                   vendor_listing=bool(vendor_listing.get(pid)))
        for fam in adapter.families:
            probe.slots[f"{pid}:{fam.name}"] = sel_mod.SlotSel(ref="")
    disc = None
    if discover is not None and probe.providers:
        try:
            disc = discover(probe)
        except Exception:  # noqa: BLE001 - discovery failing must leave the offline list, not a crash
            disc = None
    out: dict[str, list[Candidate]] = {}
    for pid in probe.providers:
        adapter = model_providers.REGISTRY[pid]
        found: list[Candidate] = []
        live = {r.provider for r in getattr(disc, "providers", []) if r.status == "ok"} if disc else set()
        if disc and pid in live:
            if pid == "anthropic":
                found += [Candidate(pid, c, i, "stable", "catalog") for c, i in disc.best.items()]
            for slot, model_id in disc.others.items():
                if slot.startswith(pid + ":"):
                    found.append(Candidate(pid, slot.split(":", 1)[1], model_id, "stable", "catalog"))
            for slot, model_id in disc.previews.items():
                if slot.startswith(pid + ":"):
                    found.append(Candidate(pid, slot.split(":", 1)[1], model_id, "preview", "catalog"))
        if not found:
            for model_id in KNOWN_MODELS.get(pid, []):
                ref = adapter.from_any_spelling(model_id)
                if ref:
                    found.append(Candidate(pid, ref.family, model_id, ref.channel, "offline list"))
        out[pid] = sorted(found, key=_order)
    return out


# --- building the selection --------------------------------------------------------------------

def choose_smoke_path(providers: list[str], backend: str | None, mode: str, accounts: dict) -> str:
    """claude-cli for a Claude-only selection on the Claude CLI; the gateway when one is chosen; else direct."""
    if providers == ["anthropic"] and mode == "single-model":
        return "claude-cli"
    if mode == "multi-model":
        return "openrouter" if backend == "openrouter" else "litellm"
    return "direct"


def build_selection(shape: str, picks: list[Candidate], primary: str | None, *, accounts: dict | None = None,
                    vendor_listing: dict[str, bool] | None = None, smoke_path: str = "claude-cli",
                    track: str = "family", allow_unverified: bool = False, engine: str = "") -> sel_mod.Selection:
    providers_in_order: list[str] = []
    for c in picks:
        if c.provider not in providers_in_order:
            providers_in_order.append(c.provider)
    s = sel_mod.Selection(shape=shape, smoke_path=smoke_path, allow_unverified=allow_unverified, openclaw_engine=engine)
    for pid in providers_in_order:
        acc = (accounts or {}).get(pid)
        s.providers[pid] = sel_mod.ProviderSel(enabled=True, detected_via=list(acc.sources) if acc else [],
                                                vendor_listing=bool((vendor_listing or {}).get(pid)))
    for c in picks:
        s.slots[c.slot] = sel_mod.SlotSel(ref=c.ref, track=track)
    s.primary = primary if primary in s.slots else (next(iter(s.slots)) if s.slots else "")
    validate(s)
    return s


def validate(s: sel_mod.Selection) -> None:
    """Raise SelectionError on a contradiction. Shape and slots must agree."""
    n = len(s.slots)
    providers = {k.split(":", 1)[0] for k in s.slots}
    if n == 0:
        raise SelectionError("no model was selected")
    if s.shape == SHAPE_SINGLE and n != 1:
        raise SelectionError(f"shape single takes exactly one model; {n} were given")
    if s.shape == SHAPE_ONE_PROVIDER and len(providers) != 1:
        raise SelectionError("shape single-provider takes models from exactly one provider; "
                             f"got {', '.join(sorted(providers))}")
    if s.shape == SHAPE_MANY_PROVIDERS and len(providers) < 2:
        raise SelectionError("shape multi-provider needs models from at least two providers")
    for slot in s.slots:
        provider = slot.split(":", 1)[0]
        if provider not in model_providers.REGISTRY or provider == "openrouter":
            raise SelectionError(f"{provider!r} is not a model provider")


def shape_of(picks: list[Candidate]) -> str:
    return sel_mod.derive_shape(sorted({c.provider for c in picks}), len(picks))


# --- flags (non-interactive) -------------------------------------------------------------------

def parse_models(spec: str, providers_hint: list[str], cands: dict[str, list[Candidate]]) -> list[Candidate]:
    """`--models a,b`: each item is `provider/id`, `provider:family` or a bare family or id that one of
    the hinted providers (else any credentialed one) owns."""
    out: list[Candidate] = []
    pool = providers_hint or list(cands)
    for raw in [x.strip() for x in spec.split(",") if x.strip()]:
        c = _resolve_model(raw, pool, cands)
        if c is None:
            raise SelectionError(f"cannot resolve model {raw!r} among {', '.join(pool) or 'no credentialed provider'}")
        out.append(c)
    return out


def _resolve_model(raw: str, pool: list[str], cands: dict[str, list[Candidate]]) -> Candidate | None:
    provider, model = (raw.split("/", 1) if "/" in raw else (None, raw))
    if ":" in raw and "/" not in raw:
        provider, model = raw.split(":", 1)
        fam = [c for c in cands.get(provider, []) if c.family == model and c.channel == "stable"]
        if fam:
            return fam[0]
        default = model_providers.REGISTRY[provider].default_id(model) if provider in model_providers.REGISTRY else None
        if default:
            return Candidate(provider, model, default, "stable", "offline list")
        return None
    for pid in ([provider] if provider else pool):
        adapter = model_providers.REGISTRY.get(pid)
        if adapter is None:
            continue
        hit = next((c for c in cands.get(pid, []) if c.id == model), None)
        if hit:
            return hit
        ref = adapter.from_any_spelling(model)
        if ref:
            return Candidate(pid, ref.family, model, ref.channel, "offline list")
        fam = [c for c in cands.get(pid, []) if c.family == model and c.channel == "stable"]
        if fam:
            return fam[0]
        default = adapter.default_id(model)
        if default:
            return Candidate(pid, model, default, "stable", "offline list")
    return None


def selection_from_args(args: argparse.Namespace, accounts: dict, cands: dict[str, list[Candidate]],
                        mode: str, backend: str | None) -> sel_mod.Selection | None:
    """The selection the flags describe, or None when none of them was given."""
    models_flag = getattr(args, "models", "") or ""
    if not models_flag:
        if getattr(args, "shape", "") or getattr(args, "providers", ""):
            raise SelectionError("--shape and --providers need --models")
        return None
    hint = [p.strip() for p in (getattr(args, "providers", "") or "").split(",") if p.strip()]
    for p in hint:
        if p not in model_providers.REGISTRY or p == "openrouter":
            raise SelectionError(f"--providers: {p!r} is not a model provider")
        acc = accounts.get(p)
        if acc is not None and not acc.credentialed:
            raise SelectionError(f"--providers: {p} has no credentials ({acc.reason})")
    picks = parse_models(models_flag, hint, cands)
    shape = getattr(args, "shape", "") or shape_of(picks)
    if shape not in sel_mod.SHAPES:
        raise SelectionError(f"--shape must be one of {', '.join(sel_mod.SHAPES)}")
    smoke = getattr(args, "smoke_path", "") or choose_smoke_path(sorted({c.provider for c in picks}), backend, mode, accounts)
    return build_selection(shape, picks, None, accounts=accounts, smoke_path=smoke,
                           allow_unverified=bool(getattr(args, "allow_unverified", False)))


# --- the plan table ----------------------------------------------------------------------------

_FILES = {
    "claude": ["~/.claude/CLAUDE.md", "~/.claude/settings.json", "~/.claude/agents/*.md", "~/.claude/commands/*"],
    "gemini": ["~/.gemini/settings.json", "~/.gemini/GEMINI.md"],
    "cursor": ["~/.cursor/AGENT_KIT.md", "~/.cursor/mcp.json"],
    "codex": ["~/.codex/AGENTS.md"],
    "aider": ["~/.aider/CONVENTIONS.md"],
    "copilot": ["~/.vscode/copilot-instructions.md"],
    "windsurf": ["~/.codeium/windsurf/memories/global_rules.md"],
    "continue": ["~/.continue/AGENT_KIT.md"],
    "opencode": ["~/.config/opencode/AGENT_KIT.md"],
    "openclaw": ["~/.openclaw/openclaw.json (through `openclaw config patch`)", "~/.openclaw/workspace/*"],
}


def files_for(action: compat.CockpitAction, mode: str) -> list[str]:
    files = list(_FILES.get(action.cockpit, []))
    if action.cockpit == "openclaw" and action.action == compat.SKIP:
        # No engine is configured for these models: openclaw.json is not patched, only the kit blocks are refreshed.
        files = [f for f in files if "openclaw.json" not in f]
    if action.cockpit == "aider" and mode == "multi-model" and action.action == compat.VIA_GATEWAY:
        files.append("~/.aider.conf.yml")
    return files


def plan_table(selection: sel_mod.Selection | None, mode: str, backend: str | None, detected: list[str],
               *, engine: str | None = None, allow_unverified: bool = False) -> list[compat.CockpitAction]:
    allow = allow_unverified or bool(getattr(selection, "allow_unverified", False))
    actions = compat.plan(selection, mode, backend, detected, engine, allow)
    for a in actions:
        a.files = files_for(a, mode) if not a.report_only else []
        if a.cockpit == "openclaw" and engine == "antigravity" and a.action == compat.CONFIGURE:
            # Antigravity runs agy's own ids, never an API id: say which one, and the risk, in the summary.
            a.model_refs = [f"{model_pins.AGY_STATIC[0]} (Antigravity id)"]
            a.notes.append("runs with unrestricted code execution; needs the risk acknowledgment")
    return actions


def _name(cid: str) -> str:
    try:
        from .cockpits import ALL
        return getattr(ALL.get(cid), "NAME", cid) if cid in ALL else cid
    except Exception:  # noqa: BLE001
        return cid


def describe(a: compat.CockpitAction) -> str:
    name = _name(a.cockpit) if not a.report_only else a.cockpit
    if a.report_only:
        return f"{name}: not configured here ({a.reason})"
    if a.action == compat.CONFIGURE:
        what = ", ".join(a.model_refs) if a.model_refs else "its own auth"
        if a.cockpit == "gemini":
            return f"{name}: uses its own model settings; ai-resources sets auth and MCP only"
        risk = f" ({'; '.join(a.notes)})" if a.notes else ""
        return f"{name}: will configure {what}{risk}"
    if a.action == compat.VIA_GATEWAY:
        label = f"through {a.gateway or 'a gateway'}"
        if a.unverified:
            label += " (not verified end to end)"
        return f"{name}: will configure {', '.join(a.model_refs)} {label}"
    if a.action == compat.INSTRUCTIONS_ONLY:
        return f"{name}: instructions only; {name} keeps its own model settings"
    return f"{name}: skipped: {a.reason}"


def render_summary(actions: list[compat.CockpitAction], selection: sel_mod.Selection | None, mode: str,
                   backend: str | None) -> list[str]:
    """The lines of the summary screen: slots, track, backend, one line per cockpit, files."""
    lines: list[str] = []
    if selection is not None:
        lines.append(f"Shape: {SHAPE_LABELS.get(selection.shape, selection.shape)}")
        for slot, sl in selection.slots.items():
            mark = " (primary)" if slot == selection.primary else ""
            lines.append(f"  {slot} = {sl.ref} [{sl.track}]{mark}")
        lines.append(f"Smoke path: {selection.smoke_path}")
    lines.append("Backend: " + (f"{backend}" if mode == "multi-model" else "none (single-model)"))
    lines.append("Cockpits:")
    for a in actions:
        lines.append("  " + describe(a))
    files = [f for a in actions for f in a.files]
    lines.append("Files that will change:")
    lines += [f"  {f}" for f in files] or ["  (none)"]
    lines.append("Note: kit content (skills, hooks, instructions) is written for every detected cockpit; "
                 "a cockpit that is not authenticated will not work until you authenticate.")
    return lines


def covers(written: list[str], listed: list[str]) -> bool:
    """True when every written path matches a listed entry and every listed (non-pattern) entry was written."""
    home = str(Path.home())
    norm = [w.replace(home, "~", 1) if w.startswith(home) else w for w in written]
    entries = [f.split(" (", 1)[0] for f in listed]
    for w in norm:
        if not any(fnmatch.fnmatch(w, e) or w.startswith(e.rstrip("*").rstrip("/") + "/") for e in entries):
            return False
    return True
