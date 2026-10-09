"""OpenClaw cockpit configurator — point a chat-facing agent at the rest of the kit.

OpenClaw is not a coding CLI: it fronts chat channels (Telegram, Slack, …) and hands
every turn to an *agent runtime*. The runtime decides how much of the kit the bot can
use, so this module's one real decision is which engine runs it:

    antigravity  runtime `agy-cli`: every turn runs the Antigravity CLI (agy, a personal
                 Google account), which hears voice notes, finds the project, and hands
                 code and team work to a `claude` worker agent that runs `claude-kit`
                 (unrestricted Claude Code, `--dangerously-skip-permissions`) with the
                 user's own tooling. This is the kit's `openclaw-plugin/ai-resources`
                 plugin, linked by setup. UNRESTRICTED CODE EXECUTION — see SECURITY.md.
    claude-code  runtime `claude-cli`: every turn runs Claude Code with the user's
                 ~/.claude settings, so the bot gets the kit's subagents, skills,
                 hooks, workflow scripts and CLAUDE.md block exactly as the
                 Claude Code cockpit installed them. Core starts this *bundled* backend
                 with `--strict-mcp-config` and `--setting-sources user`
                 (`normalizeClaudeBackendArgs`); that restriction is specific to the
                 bundled backend and does not apply to the kit's own `claude-kit`
                 backend used by the antigravity engine above.
    codex        runtime `codex`: turns run through the Codex harness, which reads
                 the Codex cockpit's ~/.codex setup.
    gemini-cli   runtime `google-gemini-cli`: not offered — Google retired CLI access
                 for personal accounts on 2026-06-18 (see ENGINES).
    keep         change nothing.

Separately, setup asks how voice notes are transcribed (see `_openclaw_voice`): the kit's
transcriber is registered as a `tools.media` CLI model and the voice rule joins the
workspace AGENTS.md block.

Antigravity serves **two independent weekly quota pools** — one for Gemini models, one
shared by Claude and GPT models (see `_agy_quota.py`) — so `ENGINES["antigravity"].models`
offers ids from both, each labelled with its pool at choice time (`_prompt_engine`).
Exhaustion is deceptive if you don't know this: agy retries a 429 `RESOURCE_EXHAUSTED`
five times with backoff (~93s total), then OpenClaw's stall detector kills the turn —
"This turn was interrupted because it stopped making progress" — and respawns it, with
no quota error ever surfaced. `ai-resources doctor` reads `agy -p "/usage"` on demand
(agy's own background quota refresh is broken) and is the only reliable signal.

With the Claude Code engine, setup also offers to mirror Claude Code's user-level MCP servers
into `mcp.servers` (see `_openclaw_mcp`), because the bot sees no others.

Verified against OpenClaw 2026.9.4. A CLI runtime's own MCP config never reaches the
bot — the servers it starts with are scoped per run (`--strict-mcp-config` for
claude-cli, `gemini-system-settings` bundling for agy-cli) — so Engram is always
declared in OpenClaw's `mcp.servers`.

Writes go through `openclaw config patch --stdin`, OpenClaw's validated writer, never
straight to openclaw.json: the gateway journals and fingerprints that file. `channels`
(Telegram allowlists, topic bindings) is never part of any patch this module builds.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import state, ui
from .. import detection
from ..detection import detect_openclaw
from ... import repo_root
from . import _shared
from . import _openclaw_mcp as mcp
from . import _openclaw_voice as voice
from . import _openclaw_host as host_section
from . import _agy_quota
from ... import model_pins, openclaw_host, openclaw_reload_rules


NAME = "OpenClaw"
ID = "openclaw"
CONFIG_ROOT = Path.home() / ".openclaw"
# How long to wait for a just-linked plugin's CLI backends to appear.
PLUGIN_ID = "ai-resources"
BACKEND_WAIT_TRIES = 15
BACKEND_WAIT_SECONDS = 2.0

VOICE_SCRIPT = "scripts/ai_resources/voice/openclaw_transcribe.py"

@dataclass(frozen=True)
class Engine:
    id: str
    label: str
    runtime: str            # OpenClaw agentRuntime.id; "" for keep
    requires: str           # cockpit/tool id that must be installed; "" for none
    models: tuple[str, ...]  # OpenClaw model refs, first is the default
    disabled_reason: str = ""  # non-"" => never offered, regardless of `requires`


ENGINES: dict[str, Engine] = {
    "antigravity": Engine(
        "antigravity",
        "Antigravity CLI (agy) — chat-facing orchestrator that hears voice notes and "
        "hands code/team work to an unrestricted Claude Code sub-agent",
        "agy-cli", "agy",
        model_pins.AGY_STATIC,
        # gemini-3.8-flash-medium leaks its reasoning into replies (S0) — never offered.
        # Antigravity serves TWO independent weekly quota pools: Gemini models share one,
        # Claude and GPT models share the other (see `_agy_quota.py`). `gemini-3.8-flash-low`
        # stays first only for backwards compatibility — it is still the default. The
        # three ids after it were verified live 2026-09-17 with a real one-token turn each
        # (`agy --model <id> -p "say ok"`): claude-sonnet-4-6 in 4.6s, claude-opus-4-6-thinking
        # in 7.2s, gpt-oss-120b-medium in 3.9s. `claude-sonnet-4-6-thinking` was tried too and
        # rejected (agy printed its model catalogue instead of running), so it is not offered.
        # `agy models` cannot be queried on this machine — its background quota refresh is
        # broken (`Singleflight refresh failed: You are not logged into Antigravity`), the
        # same auth-refresh path `_agy_quota.py`'s docstring notes for `/usage`.
    ),
    "claude-code": Engine(
        "claude-code", "Claude Code — the bot runs the full kit (subagents, skills, hooks, workflows)",
        "claude-cli", "claude",
        tuple(model_pins.openclaw_ref(model_pins.DEFAULTS[c]) for c in ("sonnet", "opus", "haiku")),
    ),
    "codex": Engine(
        "codex", "Codex CLI — the bot uses the Codex cockpit's setup",
        "codex", "codex",
        ("openai/gpt-5.6-sol", "openai/gpt-6-astra", "openai/gpt-5.6-terra"),
    ),
    "gemini-cli": Engine(
        "gemini-cli", "Gemini CLI — the bot uses the Gemini cockpit's setup",
        "google-gemini-cli", "gemini",
        ("google/gemini-3.1-pro-preview", "google/gemini-3.8-flash", "google/gemini-3.7-flash"),
        disabled_reason="not available for personal Google accounts since 2026-06-18",
    ),
    "keep": Engine("keep", "Keep OpenClaw's current engine — change nothing", "", "", ()),
}

def engine_models(engine: Engine, selection=None) -> tuple[str, ...]:
    """The refs an engine offers NOW. For claude-code the model_pins overlay is applied at call time
    (never at import), so a host pin moves the default without a kit release.

    With a recorded `selection`, claude-code and codex offer the selection's own refs for their
    provider (the primary first); antigravity always offers agy's own ids, never an API id."""
    if selection is not None and engine.id in ("claude-code", "codex"):
        provider = "anthropic" if engine.id == "claude-code" else "openai"
        slots = [k for k in selection.slots if k.startswith(provider + ":")]
        if slots:
            eff = model_pins.effective_slots(selection)
            slots.sort(key=lambda k: k != selection.primary)
            return tuple(f"{provider}/{eff[k]}" for k in slots)
    if engine.id != "claude-code":
        return engine.models
    eff = model_pins.effective()
    return tuple(model_pins.openclaw_ref(eff[c]) for c in ("sonnet", "opus", "haiku"))


def selection_engines(s: state.SetupState) -> list[str] | None:
    """Engine ids the compatibility matrix allows for the recorded selection (None: no selection).

    Antigravity is returned as a separate, labelled choice and is never the default."""
    selection = s.get_selection() if hasattr(s, "get_selection") else None
    if selection is None:
        return None
    from .. import compat
    detected = [cid for cid, cs in s.cockpits.items() if cs.installed]
    return compat.engines_for(selection, detected, allow_unverified=selection.allow_unverified)


def default_worker_model() -> str:
    """The bare id the claude worker agent defaults to: the effective sonnet pin."""
    return model_pins.effective()["sonnet"]


RUNTIME_IDS = {e.runtime for e in ENGINES.values() if e.runtime}


def detect():
    return detect_openclaw()


def available_engines(s: state.SetupState) -> list[Engine]:
    """Engines whose backing CLI is installed and not permanently disabled, in preference order."""
    out = []
    allowed = selection_engines(s)
    for eng in ENGINES.values():
        if eng.disabled_reason:
            continue
        if eng.requires and not (s.cockpits.get(eng.requires) or state.CockpitState()).installed:
            continue
        if allowed is not None and eng.id != "keep" and eng.id not in allowed:
            continue
        out.append(eng)
    return out


def default_engine(s: state.SetupState) -> str:
    """The saved answer, else antigravity when agy is installed, else Claude Code, else keep."""
    ids = [e.id for e in available_engines(s)]
    if s.openclaw.engine in ids:
        return s.openclaw.engine
    if s.openclaw.engine == "direct":
        # The "direct" engine (OpenClaw's own runtime, wired to OpenRouter) was removed
        # from the kit; OpenRouter itself is still supported (see multi-model setup),
        # just no longer wired through this module. Fall back rather than crash.
        ui.warn("OpenClaw's 'direct' engine was removed from the kit — falling back to "
                "'keep'. OpenRouter is still available as a multi-model backend.")
        return "keep"
    if ui.is_non_interactive():
        # Never repoint a live bot from a first unattended run.
        return "keep"
    if selection_engines(s) is not None:
        # A recorded selection: the first engine it supports; antigravity is never the default.
        return next((i for i in ids if i not in ("antigravity", "keep")), "keep")
    if "antigravity" in ids:
        return "antigravity"
    return "claude-code" if "claude-code" in ids else "keep"


def openrouter_selected(s: state.SetupState) -> bool:
    """Whether the kit's own multi-model setup routes through OpenRouter."""
    return s.mode == "multi-model" and s.backend == "openrouter"


# --- config IO -------------------------------------------------------------------

def _openclaw(args: list[str], stdin: str | None = None, timeout: int = 120) -> tuple[int, str]:
    binary = shutil.which("openclaw")
    if not binary:
        return 127, "openclaw not found on PATH"
    try:
        r = subprocess.run([binary, *args], input=stdin, capture_output=True, text=True,
                           timeout=timeout, cwd=str(Path.home()))
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


def _agy(args: list[str], timeout: int = 30) -> tuple[int, str]:
    # Not plain shutil.which: agy's official installer defaults to ~/.local/bin, which
    # a non-login systemd/service PATH may not see (same reasoning as detect_agy()).
    binary = detection._which_extra("agy")
    if not binary:
        return 127, "agy not found on PATH"
    try:
        r = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


def config_path() -> Path:
    env = os.environ.get("OPENCLAW_CONFIG_PATH")
    if env:
        return Path(env).expanduser()
    rc, out = _openclaw(["config", "file"], timeout=60)
    if rc == 0 and out:
        return Path(out.splitlines()[-1].strip()).expanduser()
    return CONFIG_ROOT / "openclaw.json"


def read_config(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # JSON5 the kit cannot parse: behave as if empty and let OpenClaw validate the patch.
        return {}


def _get(doc: dict, *keys: str) -> Any:
    cur: Any = doc
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def snapshot(doc: dict) -> dict:
    """What the kit is about to overwrite, so teardown can put it back exactly.

    Captures a superset of keys any engine's build_patch might touch; unused keys for a
    given engine just stay `None` and restoring them on teardown is a no-op.
    """
    return {
        "model": _get(doc, "agents", "defaults", "model"),
        "models": _get(doc, "agents", "defaults", "models"),
        "engram": _get(doc, "mcp", "servers", "engram"),
        "extra_dirs": _get(doc, "skills", "load", "extraDirs"),
        "model_policy": _get(doc, "agents", "defaults", "modelPolicy"),
        "default_agent_runtime": _get(doc, "agents", "defaults", "agentRuntime"),
        "default_allow_agents": _get(doc, "agents", "defaults", "subagents", "allowAgents"),
        "agent_entries": _get(doc, "agents", "entries"),
        "openrouter_plugin": _get(doc, "plugins", "entries", "openrouter"),
        "llama_cpp_plugin": _get(doc, "plugins", "entries", "llama-cpp"),
        "ai_resources_plugin_config": _get(doc, "plugins", "entries", "ai-resources"),
    }


def _allowed(model: str, allow: list) -> bool:
    return any(a == model or (isinstance(a, str) and a.endswith("*") and model.startswith(a[:-1]))
               for a in allow)


def _openrouter_plugin_fragment(doc: dict, openrouter_enabled: bool) -> dict:
    """`plugins.entries.openrouter` + alias cleanup, shared by every engine (AC-13b)."""
    frag: dict[str, Any] = {}
    current = _get(doc, "plugins", "entries", "openrouter", "enabled")
    if current != openrouter_enabled:
        frag["plugins"] = {"entries": {"openrouter": {"enabled": openrouter_enabled}}}
    if not openrouter_enabled:
        models = _get(doc, "agents", "defaults", "models") or {}
        stale = {ref: None for ref in models if isinstance(ref, str) and ref.startswith("openrouter/")}
        if stale:
            frag.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {}).update(stale)
    return frag


def _deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def build_patch(engine: Engine, model: str, doc: dict, kit_skills: str, engram_command: str, *,
                openrouter_enabled: bool = True,
                worker_model: str = "", ak_path: str = "", agy_bin: str = "", claude_bin: str = "",
                python3_bin: str = "") -> dict:
    """The openclaw.json patch that points the default agent at `engine`."""
    if engine.id == "antigravity":
        patch = _build_antigravity_patch(
            model, worker_model, doc, engram_command,
            agy_bin=agy_bin, claude_bin=claude_bin, python3_bin=python3_bin,
        )
    else:
        patch = {
            "agents": {"defaults": {
                "model": {"primary": model},
                "models": {model: {"agentRuntime": {"id": engine.runtime}}},
            }},
        }
        # Runtimes are attached per model ref; a ref the kit pinned to another runtime
        # earlier would keep that pin if the user later picked it by hand.
        for ref, cfg in (_get(doc, "agents", "defaults", "models") or {}).items():
            if ref == model or not isinstance(cfg, dict):
                continue
            rid = _get(cfg, "agentRuntime", "id")
            if rid in RUNTIME_IDS and rid != engine.runtime:
                patch["agents"]["defaults"]["models"][ref] = {"agentRuntime": None}

        # With an allowlist present the primary still runs, but nobody could pick it from
        # chat; without one every model is selectable and there is nothing to add.
        allow = _get(doc, "agents", "defaults", "modelPolicy", "allow")
        if isinstance(allow, list) and not _allowed(model, allow):
            patch["agents"]["defaults"]["modelPolicy"] = {"allow": [*allow, model]}

        if not _get(doc, "mcp", "servers", "engram"):
            patch["mcp"] = {"servers": {"engram": {
                "command": engram_command, "args": ["mcp", "--tools=agent"],
                "supportsParallelToolCalls": True,
            }}}

        # No remaining engine writes kit skills into OpenClaw's own runtime (the
        # "direct" engine that used to did was removed); this only fires as cleanup
        # for a config an older kit version left with kit_skills still in extraDirs.
        existing_dirs = _get(doc, "skills", "load", "extraDirs") or []
        dirs = [d for d in existing_dirs if d != kit_skills]
        if dirs != existing_dirs:
            patch["skills"] = {"load": {"extraDirs": dirs or None}}

    # AC-13b: the openrouter plugin toggle applies to every engine build_patch handles.
    _deep_merge(patch, _openrouter_plugin_fragment(doc, openrouter_enabled))
    return patch


def _build_antigravity_patch(model: str, worker_model: str, doc: dict, engram_command: str, *,
                             agy_bin: str, claude_bin: str, python3_bin: str) -> dict:
    engine = ENGINES["antigravity"]
    primary_ref = f"{engine.runtime}/{model}"
    patch: dict[str, Any] = {
        "agents": {
            "defaults": {
                # No `agentRuntime` key here: it is not in OpenClaw's agent schema
                # (`openclaw config patch --dry-run` rejects it). The CLI backend is
                # resolved from the model ref's first segment, as the S0 spike proved.
                "model": {"primary": primary_ref},
            },
            "entries": {
                "claude": {
                    "name": "claude",
                    # A wide "projects root" workspace (user-confirmed 2026-09-17), not
                    # `operator.admin`: sessions_spawn cwds under ~/Development stay
                    # inside the configured workspace without the wider grant.
                    "workspace": str(Path.home() / "Development"),
                    # Namespaced with the runtime id, same convention as the default
                    # agent's `agy-cli/<model>` ref (architect decision #5/#1): OpenClaw
                    # resolves the backend from the first path segment.
                    "model": {"primary": f"claude-kit/{worker_model}"},
                    # `tools.exec.mode` is the schema's policy field; `enabled`/`ask:
                    # false` are rejected, and `mode` cannot be combined with `ask`.
                    "tools": {"exec": {"mode": "full"}},
                },
            },
        },
    }

    # subagents.allowAgents must include "claude": on every agent already declared in
    # the live config, and on the defaults so a future agent inherits it too.
    entries = _get(doc, "agents", "entries") or {}
    for name, cfg in entries.items():
        if name == "claude":
            continue
        existing_allow = _get(cfg if isinstance(cfg, dict) else {}, "subagents", "allowAgents") or []
        if "claude" not in existing_allow:
            patch["agents"]["entries"][name] = {"subagents": {"allowAgents": [*existing_allow, "claude"]}}
    default_allow = _get(doc, "agents", "defaults", "subagents", "allowAgents") or []
    if "claude" not in default_allow:
        patch["agents"]["defaults"]["subagents"] = {"allowAgents": [*default_allow, "claude"]}

    if not _get(doc, "mcp", "servers", "engram"):
        patch["mcp"] = {"servers": {"engram": {
            "command": engram_command, "args": ["mcp", "--tools=agent"],
            "supportsParallelToolCalls": True,
        }}}



    plugin_config: dict[str, str] = {}
    if agy_bin:
        plugin_config["agyBin"] = agy_bin
    if claude_bin:
        plugin_config["claudeBin"] = claude_bin
    if python3_bin:
        plugin_config["python3Bin"] = python3_bin
    if plugin_config:
        patch["plugins"] = {"entries": {"ai-resources": {"config": plugin_config}}}

    # channels / channels.telegram is deliberately never referenced anywhere above.
    return patch


def apply_patch(patch: dict, *, dry_run: bool = False,
                replace_paths: list[str] | None = None) -> tuple[bool, str]:
    # The Telegram allowlist is the only gate in front of the unrestricted claude-kit backend:
    # no patch may carry an allowlist or a topic binding, and no --replace-path may name channels.
    openclaw_host.assert_channels_safe(patch, replace_paths)
    args = ["config", "patch", "--stdin"] + (["--dry-run"] if dry_run else [])
    for rp in (replace_paths or []):
        args += ["--replace-path", rp]
    rc, out = _openclaw(args, stdin=json.dumps(patch))
    return rc == 0, out


def _antigravity_restore_fragment(prev: dict) -> tuple[dict, list[str]]:
    """Patch fragment + `--replace-path`s that undo everything only the antigravity
    engine's `_build_antigravity_patch` touches.

    Shared by `configure()` (switching from antigravity to a different engine) and
    `teardown()`, both of which must restore these keys whenever the antigravity engine
    was actually applied at some point — regardless of which engine is selected *now*
    (see `OpenClawState.antigravity_applied`).
    """
    frag: dict[str, Any] = {
        "agents": {
            "defaults": {
                "subagents": {"allowAgents": prev.get("default_allow_agents")},
            },
            "entries": prev.get("agent_entries"),
        },
        "plugins": {"entries": {"ai-resources": prev.get("ai_resources_plugin_config")}},
    }
    replace_paths = ["agents.entries"]
    return frag, replace_paths


MEMORY_MD = """## Memory

- **Engram is the memory of record.** Before answering anything that may depend on earlier work,
  call `mem_search`; after a decision, a fix, a discovery or a stated preference, call `mem_save`.
  Engram is shared with every other cockpit on this machine, so what the bot saves here is what
  Claude Code, Codex or Gemini CLI recall later, and the other way round.
- OpenClaw's own memory (MEMORY.md, memory files, transcript recall) is recent chat context only.
  Do not treat it as the durable store, and do not copy Engram content into it.
"""


def _antigravity_agents_md(ak_path: str) -> str:
    return (
        "# ai-resources (OpenClaw — antigravity)\n\n"
        f"Kit root: `{ak_path}`. After `brew upgrade ai-resources`, run `ai-resources setup`.\n\n"
        "## Orchestration\n\n"
        "- You (agy) are the orchestrator for every chat topic. You talk, hear voice notes, "
        "find the right project, and reply directly for anything that is not code or team work.\n"
        "- Projects live under `~/Development/**` (wildbit and globant/pichincha). Ask before "
        "touching a Banco Pichincha project.\n"
        "- Any `kubectl` use must pass an explicit `--context` — never rely on the current "
        "context, which changes when the operator runs `az aks get-credentials`.\n\n"
        "## Delegation\n\n"
        "- Hand code and team work to the `claude` worker agent: "
        "`sessions_spawn agentId=claude cwd=<project> thread=true`, so the reply lands back "
        "in the same Telegram thread.\n"
        "- `/claude` and `/equipo` are shortcuts to the same worker agent; if the shortcut "
        "command itself is unavailable on this gateway, call `sessions_spawn agentId=claude` "
        "yourself for any message starting with `/claude` or `/equipo`.\n\n"
        "## Voice\n\n"
        "- If a voice note could not be transcribed you will see the marker "
        "`[voice note could not be understood]`. Relay it to the user as: "
        "\"I could not understand the voice note, please resend it or type it.\"\n\n"
        f"{MEMORY_MD}"
    )


def workspace_dir(doc: dict) -> Path:
    ws = _get(doc, "agents", "defaults", "workspace")
    return Path(ws).expanduser() if ws else CONFIG_ROOT / "workspace"


def _remove_managed_block(path: Path) -> bool:
    return _shared.remove_managed_block(path)


# --- agy MCP bridge (antigravity only) --------------------------------------------

def _agy_mcp_has_openclaw_entry() -> bool:
    rc, out = _agy(["mcp", "list"])
    return rc == 0 and "openclaw" in out


def register_agy_mcp_bridge(python3_bin: str, bridge_path: str) -> tuple[bool, bool, str]:
    """Register the kit's stdio MCP bridge with agy.

    Returns (changed, pre_existed, output). Never overwrites a user's own `openclaw`
    MCP entry: if one is already registered, this is a no-op and `pre_existed` is True,
    so teardown knows not to remove it later. `output` carries `agy`'s stderr/stdout so
    a failed registration (e.g. `agy` not resolvable, 127) can be reported instead of
    swallowed.
    """
    if _agy_mcp_has_openclaw_entry():
        return False, True, ""
    rc, out = _agy(["mcp", "add", "openclaw", python3_bin, bridge_path])
    return rc == 0, False, out


def unregister_agy_mcp_bridge() -> bool:
    rc, _out = _agy(["mcp", "remove", "openclaw"])
    return rc == 0


# --- setup entry points ------------------------------------------------------------

def prompt(s: state.SetupState, *, dry_run: bool = False) -> None:
    """Ask which engine runs the bot, which model it uses, which MCP servers it gets and how it
    hears voice notes. Called from step 7."""
    _prompt_engine(s, dry_run=dry_run)
    if s.openclaw.engine == "claude-code":
        mcp.prompt(s, _get(read_config(config_path()), "mcp", "servers") or {})
    voice.prompt(s)
    host_section.prompt(s, read_config(config_path()))
    _prompt_stale_check(s)


STALE_CHECKS = ("linked", "engine", "off")


def _prompt_stale_check(s: state.SetupState) -> None:
    """Ask when `ai-resources doctor` reports a gateway running older plugin code (doctor 4c)."""
    saved = s.openclaw.stale_plugin_check
    ui.detail("After `brew upgrade` the gateway keeps running the plugin code it loaded at start "
              "until it is restarted.")
    ui.detail("The default can make `ai-resources doctor` report an issue, and so exit non-zero, "
              "on a host where it said nothing before. It only reports; it never restarts.")
    s.openclaw.stale_plugin_check = ui.select(
        "When should `ai-resources doctor` report that?",
        [ui.Choice("Whenever the kit's plugin is linked (default)", value="linked"),
         ui.Choice("Only when the kit manages the engine (behaviour before this version)", value="engine"),
         ui.Choice("Never", value="off")],
        default=saved if saved in STALE_CHECKS else "linked",
    )


def _prompt_engine(s: state.SetupState, *, dry_run: bool = False) -> None:
    engines = available_engines(s)
    missing = [e for e in ENGINES.values()
              if e.requires and not e.disabled_reason and e not in engines
              and not (s.cockpits.get(e.requires) or state.CockpitState()).installed]
    if missing:
        ui.detail("Not offered (CLI not installed): " + ", ".join(e.id for e in missing))
    disabled = [e for e in ENGINES.values() if e.disabled_reason]
    for e in disabled:
        ui.detail(f"Not offered: {e.id} — {e.disabled_reason}")

    engine_id = ui.select(
        "Which engine should OpenClaw run its agents on?",
        [ui.Choice(e.label, value=e.id) for e in engines],
        default=default_engine(s),
    )
    s.openclaw.engine = engine_id
    engine = ENGINES[engine_id]

    if engine_id == "antigravity":
        # A vendor-prefixed value saved by an earlier run would build a three-segment
        # ref, which OpenClaw cannot resolve; keep only the model id.
        saved_worker = (s.openclaw.worker_model or default_worker_model()).split("/")[-1]
        s.openclaw.worker_model = ui.text(
            "Model for the unrestricted claude worker agent:", default=saved_worker,
        ) or saved_worker

    models = engine_models(engine, s.get_selection() if hasattr(s, "get_selection") else None)
    if not models:
        return
    saved = s.openclaw.model if s.openclaw.model in models else models[0]

    if engine_id == "antigravity":
        # Antigravity serves two independent weekly quota pools (see `_agy_quota.py`);
        # label each choice with its pool. `value=` stays the bare id so the patch and
        # state formats are untouched — only the label changes.
        choices = [
            ui.Choice(f"{m} — {_agy_quota.pool_for_model(m)} pool", value=m)
            for m in models
        ]
        # Best-effort live pool state, read-only, at most once, never blocking:
        # skipped under non-interactive and dry-run so an unattended run stays
        # byte-identical to today and never gains a subprocess that can hang on a
        # machine where agy is not signed in.
        agy_bin = (not ui.is_non_interactive() and not dry_run
                   and detection._which_extra("agy"))
        if agy_bin:
            # Pass the resolved absolute path: agy commonly lives only in ~/.local/bin,
            # which _which_extra() searches but the inherited PATH may not carry. Letting
            # read_usage() fall back to a bare "agy" would make the annotation vanish
            # silently on exactly the machines the extra-bin lookup exists for.
            pools, _reason = _agy_quota.read_usage(agy_bin=agy_bin)
            # report()'s severity is scoped to an applied model (doctor's job, S4); here
            # nothing is chosen yet, so any pool at 0% warns on sight — reuse report()
            # only for its message text, one call per pool, so the wording never drifts.
            for pool in pools:
                message = _agy_quota.report([pool])[0][1]
                if pool.remaining_pct == 0:
                    ui.warn(f"{message} — {_agy_quota.remedy(pools, pool.name)}")
                else:
                    ui.detail(message)
    else:
        choices = [ui.Choice(m, value=m) for m in models]

    s.openclaw.model = ui.select(
        "Model for OpenClaw's default agent:",
        choices,
        default=saved,
    )
    # OpenClaw hands Claude Code the bare Anthropic ID (claude-sonnet-5) whatever the
    # ref says. That works under either gateway: LiteLLM registers bare Claude IDs,
    # and under OpenRouter the Claude Code cockpit maps them with `modelOverrides`.

    if engine_id == "antigravity":
        ui.warn("agy and the claude worker agent both run with --dangerously-skip-permissions: "
                "anyone who reaches this bot (Telegram allowFrom) gets unrestricted code "
                "execution as you, including this host's kubeconfigs and MCP credentials. "
                "See SECURITY.md.")
        s.openclaw.risk_acknowledged = ui.confirm(
            "Acknowledge unrestricted code execution and continue?",
            default=s.openclaw.risk_acknowledged,
        )


def applied_engine(s: state.SetupState) -> Engine | None:
    """The engine the kit's last write pointed OpenClaw at, None when it never wrote one."""
    if not s.openclaw.applied:
        return None
    for eng in ENGINES.values():
        if s.openclaw.model in eng.models or s.openclaw.model in engine_models(eng):
            return eng
    return None


def configure(ctx: dict) -> list[Path]:
    s = ctx["state"]
    dry_run = ctx.get("dry_run", False)
    written: list[Path] = []

    cs = s.cockpits.get(ID) or state.CockpitState()
    cs.installed = True
    cs.version = ctx.get("detected_version", cs.version)
    cs.binary_path = ctx.get("detected_path", cs.binary_path)
    cs.config_root = str(CONFIG_ROOT)

    path = config_path()
    doc = read_config(path)
    ak_path = str(_shared.stable_kit_root(repo_root()))

    engine_changed = _configure_engine(ctx, doc, path, ak_path, written)
    mcp_changed = _configure_mcp(s, doc, path, written, dry_run=dry_run)
    voice_changed = _configure_voice(s, doc, path, ak_path, written, dry_run=dry_run)
    workshop_changed = _configure_skill_workshop(s, doc, path, written, dry_run=dry_run)
    # Above the early return below on purpose: the host section's local work (kit-host.env, hooks,
    # AGENTS.md) does not depend on openclaw.json having changed, and it renders its own dry run.
    host_changed = host_section.configure(s, doc, ak_path, written, dry_run=dry_run,
                                          apply_patch=apply_patch, oc=_openclaw, config_path=path)
    changed = engine_changed or mcp_changed or voice_changed or workshop_changed or host_changed
    # The kit block in every agent workspace does not depend on the engine, on the host section's
    # answers or on openclaw.json having changed: it is refreshed on every run (B1, v1.9.7).
    _configure_workspace_blocks(s, doc, ak_path, ctx.get("gateway_url", ""), written,
                                dry_run=dry_run, force_default=changed)
    if dry_run or not changed:
        return written

    cs.configured = True
    cs.last_configured_at = datetime.now(timezone.utc).isoformat()
    s.cockpits[ID] = cs
    return written


def plugin_runtime() -> dict:
    """The running gateway's view of the kit plugin; {} when it cannot be read.

    NOT `openclaw models list`: that lists provider models and never prints CLI-backend
    refs, so it reports "missing" for a backend the gateway is happily using (seen on a
    real run, 2026-09-17). The plugin's runtime inspection is the honest source.
    """
    rc, out = _openclaw(["plugins", "inspect", PLUGIN_ID, "--runtime", "--json"])
    if rc != 0:
        return {}
    start = out.find("{")
    if start < 0:
        return {}
    try:
        report, _ = json.JSONDecoder().raw_decode(out[start:])
    except ValueError:
        return {}
    return (report.get("plugin") or {}) if isinstance(report, dict) else {}


def plugin_loaded() -> bool:
    """Whether the running gateway has the kit plugin loaded, however it got linked.

    Observes the gateway; it does not read `plugin_linked`, which only records that THE KIT ran
    the link (inside the antigravity registration). A host on engine `keep` can carry a plugin
    linked by an earlier run or by hand, and the gateway is the only one that knows. An unreadable
    runtime is {}, so this is False: a warning must never rest on a guess.
    """
    return plugin_runtime().get("status") == "loaded"


def backend_registered(backend: str) -> bool:
    """Whether the running gateway has loaded the plugin that registers `backend`."""
    plugin = plugin_runtime()
    return plugin.get("status") == "loaded" and backend in (plugin.get("cliBackendIds") or [])


def _gateway_started_at() -> float | None:
    return openclaw_host.gateway_started_at()


def _installed_at(plugin_dir: Path) -> float | None:
    """When the kit behind `plugin_dir` landed on disk; None when it cannot be told.

    NOT the mtime of the plugin directory or of its files: Homebrew preserves the source mtimes of
    everything it installs, so those predate the install by weeks (measured 2026-09-29: plugin dir
    2026-09-25, index.js 2026-09-17, on a kit installed 2026-09-29 14:41) and a comparison against
    them says "older than the gateway" on exactly the upgrade this rule exists to catch.

    The plugin dir is `<kit root>/openclaw-plugin/ai-resources`, resolved first so the
    version-independent opt/ link is followed to the real Cellar directory. Two directories above
    it are read, and NEITHER is redundant:

      * the kit root (`<Cellar>/ai-resources/<version>/libexec`) is fresh only on a SOURCE build,
        and only by accident: `Formula/ai-resources.rb` (`libexec.install Dir["*"]`, then
        `virtualenv_create(libexec/"venv", ...)`) adds the venv inside it after the copy. On a
        poured bottle tar restores directory mtimes, so libexec carries the BUILD MACHINE's time,
        which can predate the local gateway start.
      * the version directory (`<Cellar>/ai-resources/<version>`) is the one that stays fresh on a
        bottle, because brew writes `INSTALL_RECEIPT.json` into it after extracting. Dropping it
        as "redundant" reintroduces the 1.10.2 -> 1.11.0 false negative on every bottle install.
        It is only read when the tree really is `<...>/Cellar/<formula>/<version>/libexec` (the
        same literal `stable_kit_root` relies on); anywhere else, such as a repo checkout, only
        the kit root counts, so the parent directory of a checkout is never mistaken for an
        install. A Cellar that does not match that shape silently loses the bottle coverage.

    Accepted false positives, all of which fail toward "restart the gateway", never toward hiding
    an upgrade: brew rewrites the receipt atomically on `brew pin`, `brew unpin` and
    `brew link --overwrite`, which bumps the version directory's mtime without changing any code;
    and a repo checkout moves the kit root's mtime whenever a top-level entry is added or removed.
    """
    try:
        kit_root = plugin_dir.resolve().parents[1]
        stamps = [kit_root.stat().st_mtime]
        version_dir = kit_root.parent
        if version_dir.parent.parent.name == "Cellar":
            stamps.append(version_dir.stat().st_mtime)
        return max(stamps)
    except (OSError, IndexError):
        return None


def plugin_is_stale(plugin_dir: str, runner=None) -> bool:
    """Whether the gateway is running older plugin code than the kit has on disk; see `stale_reason`."""
    return stale_reason(plugin_dir, runner) is not None


def stale_reason(plugin_dir: str, runner=None) -> str | None:
    """Which rule says the gateway runs older plugin code than the kit has on disk: "path", "time",
    or None when neither does. The two need different words for the operator: on "time" the paths
    are identical, so a message about an older directory sends them comparing equal strings.

    Two rules, either one is enough:

      * PATH: the runtime root is not the expected plugin dir. `brew upgrade` moves the kit into
        a new Cellar directory, so a gateway holding the old versioned path answers `Unknown CLI
        backend` to every message until it restarts (seen after upgrading to 1.7.2 on 2026-09-17).
      * TIME: the installed kit is newer than the gateway process. Setup links the plugin through
        the version-independent opt/ path on purpose, so after an upgrade both sides of the path
        rule are equal (opt/ simply resolves to the new Cellar directory) and that rule is blind:
        the gateway kept its 1.10.2 code in memory through the 1.11.0 upgrade and nothing said so
        (2026-09-29). The unit's start time against the install time of the RESOLVED kit root catches
        it (see `_installed_at`: not the plugin dir, whose mtime brew preserves from the source).

    The time rule fails open, like every probe here: an unreadable start time or mtime leaves the
    path rule to decide alone. A working repo checkout trips it whenever a top-level entry of the
    kit root is added or removed (a directory's mtime does not move on edits to files inside it),
    because there the gateway genuinely IS running older code than the tree on disk.

    That is not only a warning: `configure()` calls this and RESTARTS the gateway when it is True,
    so a developer running setup from a checkout gets a restart (behind the busy guard) after
    such a change. `brew pin` also trips it; see `_installed_at`.
    """
    root = (plugin_runtime().get("rootDir") or "").strip()
    if not root:
        return None
    try:
        if Path(root).resolve() != Path(plugin_dir).resolve():
            return "path"
    except OSError:
        if root != plugin_dir:
            return "path"
    # An explicit runner is the caller's fake (verify threads its own): it must never reach the live unit.
    started = openclaw_host.gateway_started_at(runner) if runner else _gateway_started_at()
    if started is None:
        return None
    installed = _installed_at(Path(plugin_dir))
    return "time" if installed is not None and installed > started else None


# Limits of the restart guard that are accepted, not overlooked:
#   * a turn that starts between the last poll and `gateway restart` is still killed: the probe
#     narrows the window, it cannot close it without locking the gateway;
#   * the 30-minute wait cap is a constant, not a setting;
#   * a non-interactive upgrade on an always-busy host defers on EVERY run and leaves the plugin
#     stale until someone acts. That is the design: the alternative is killing work unattended,
#     and the loud error plus `ai-resources openclaw status` are the nudge.
#
# The three answers when agent turns are in flight. `wait` is what an operator does by hand, and
# the only one that ends with both the work finished and the tooling correct.
RESTART_NOW = "restart now (kills the turns listed above)"
RESTART_DEFER = "defer (leave the gateway running; setup reports what is left to do)"
RESTART_WAIT = "wait until they finish, then restart"
RESTART_WAIT_CAP = 30 * 60        # seconds `wait` polls before it falls back to defer
RESTART_WAIT_INTERVAL = 15.0
RESTART_COMMAND = "openclaw gateway restart"


class RestartDeferred(Exception):
    """The gateway was NOT restarted because agent turns are in flight. Never a silent skip: the
    caller records it and reports OpenClaw as not complete."""

    def __init__(self, reason: str, busy: list):
        super().__init__(reason)
        self.reason, self.busy = reason, busy


def _gateway_busy() -> tuple[int, list]:
    return openclaw_host.gateway_busy(out=ui.detail)


def _gateway_main_pid() -> str:
    return openclaw_host.gateway_main_pid()


def _can_ask() -> bool:
    """Whether anyone can answer a prompt: the wizard's own flag, then a terminal on stdin."""
    return not ui.is_non_interactive() and ui.stdin_is_a_terminal()


def _probe_busy(probe) -> tuple[int, list]:
    """The probe already swallows its own errors; this is the belt to that pair of braces, because
    a restart guard that raises would block the upgrade it exists to protect."""
    try:
        return (probe or _gateway_busy)()
    except Exception as e:  # noqa: BLE001
        ui.detail(f"restart guard: the busy probe failed ({type(e).__name__}: {e}); treating the gateway as idle")
        return 0, []


def _await_idle(probe, count: int, workers: list, *, sleep, monotonic, cap: float, interval: float):
    """Poll until no agent turn is left; RestartDeferred at the cap or on Ctrl-C.

    An interrupted wait defers and never restarts: the operator who pressed Ctrl-C wanted out."""
    deadline = monotonic() + cap
    try:
        while count:
            if monotonic() >= deadline:
                raise RestartDeferred(f"{count} agent turn(s) still in flight after waiting {cap / 60:g} min", workers)
            sleep(interval)
            count, workers = _probe_busy(probe)
    except KeyboardInterrupt:
        raise RestartDeferred(f"{count} agent turn(s) in flight (wait interrupted)", workers) from None


def restart_gateway(backend: str = "agy-cli", *, probe=None, ask=None, sleep=None, monotonic=None,
                    interactive: bool | None = None, wait_cap: float = RESTART_WAIT_CAP,
                    wait_interval: float = RESTART_WAIT_INTERVAL, restart_timeout: int = 180) -> bool:
    """Restart the gateway and report whether the backend came back registered.

    A restart kills every agent turn in flight, and the caller reaches this exactly when
    `brew upgrade` moved the plugin, so it looks first. Idle: restart as it always did, no
    prompt. Busy and interactive: ask (restart now / defer / wait). Busy and not interactive:
    defer. A deferral raises RestartDeferred; it never returns quietly.
    """
    count, workers = _probe_busy(probe)
    if count:
        reason = f"{count} agent turn(s) in flight"
        if not (_can_ask() if interactive is None else interactive):
            raise RestartDeferred(reason, workers)
        try:
            answer = (ask or ui.select)(
                f"OpenClaw is running {count} agent turn(s); a restart kills them:\n"
                f"{openclaw_host.describe_busy(workers)}\nWhat now?",
                [RESTART_NOW, RESTART_DEFER, RESTART_WAIT], default=RESTART_DEFER)
        except KeyboardInterrupt:
            answer = None
        if answer == RESTART_WAIT:
            ui.info(f"Waiting up to {wait_cap / 60:g} min for the turns to finish (Ctrl-C defers)...")
            _await_idle(probe, count, workers, sleep=sleep or time.sleep, monotonic=monotonic or time.monotonic,
                        cap=wait_cap, interval=wait_interval)
        elif answer != RESTART_NOW:
            raise RestartDeferred(reason, workers)
    rc, out = _openclaw(["gateway", "restart"], timeout=restart_timeout)
    if rc != 0:
        ui.warn(f"OpenClaw: `gateway restart` failed ({out[-200:]}).")
        return False
    for _ in range(BACKEND_WAIT_TRIES):
        if backend_registered(backend):
            return True
        time.sleep(BACKEND_WAIT_SECONDS)
    return False


def _record_deferred_restart(s: state.SetupState, deferred: RestartDeferred) -> None:
    """Write the gap down and say it loudly: setup must not claim a state it does not have."""
    s.openclaw.gateway_restart_pending = {
        "reason": deferred.reason, "command": RESTART_COMMAND, "main_pid": _gateway_main_pid(),
        "since": datetime.now(timezone.utc).isoformat()}
    ui.error(f"OpenClaw setup is NOT complete: the gateway restart was deferred ({deferred.reason}), so the "
             "gateway still runs the old plugin and the agy-cli backend is not registered. "
             f"Finish with `{RESTART_COMMAND}` once the turns have ended; "
             "`ai-resources openclaw status` shows this until it is done.")


def pending_restart_findings(o: state.OpenClawState, main_pid=None) -> list:
    """The verify finding for a deferred restart that has not happened; [] when none is owed.

    Done means the unit's MainPID changed since the deferral (see `restart_done`). An unreadable
    pid is not done, and neither is a malformed record: a corrupt state must not read as
    "nothing is owed"."""
    from ...verify import Finding as F

    if o.gateway_restart_pending is None or o.gateway_restart_pending == {}:
        return []
    pending = openclaw_host.normalize_pending(o.gateway_restart_pending)
    if openclaw_host.restart_done(pending, (main_pid or _gateway_main_pid)()):
        return []
    return [F("error", ID, f"the gateway restart setup deferred ({pending.get('reason', '')}) has not happened",
              f"run `{pending.get('command', RESTART_COMMAND)}` when the agent turns have ended")]


def _register_plugin_and_backends(s: state.SetupState, ak_path: str) -> bool:
    """Link the kit plugin, register the agy MCP bridge, and make the gateway load them.

    This MUST run before the engine patch: `openclaw config patch` validates model
    references, and `agy-cli/<model>` is unknown until the plugin that registers the
    backend is both linked and loaded by a (re)started gateway — a fresh setup run
    otherwise fails with "Unknown model: agy-cli/…" (seen on a real run, 2026-09-17).
    Returns whether the backends are usable.
    """
    s.openclaw.antigravity_applied = True
    if not s.openclaw.plugin_linked:
        plugin_dir = str(Path(ak_path) / "openclaw-plugin" / "ai-resources")
        rc_link, out_link = _openclaw([
            "plugins", "install", "--link", "--force", "--accept-capabilities", plugin_dir])
        if rc_link == 0:
            s.openclaw.plugin_linked = True
        else:
            ui.warn(f"OpenClaw: could not link the ai-resources plugin ({out_link[-200:]})")

    if "agy_mcp_bridge_preexisted" not in (s.openclaw.previous or {}):
        bridge_path = str(Path(ak_path) / "openclaw-plugin" / "ai-resources" / "bridge.py")
        python3_bin = shutil.which("python3") or "python3"
        changed, pre_existed, bridge_out = register_agy_mcp_bridge(python3_bin, bridge_path)
        if changed or pre_existed:
            s.openclaw.previous["agy_mcp_bridge_preexisted"] = pre_existed
        else:
            # Do NOT record the marker on failure: leaving it unset means the next
            # `configure()` run retries registration instead of silently giving up
            # forever (the guard above only skips once the key is actually present).
            ui.warn(f"OpenClaw: could not register the agy MCP bridge "
                    f"({bridge_out[-200:]}); will retry on the next run.")

    plugin_dir = str(Path(ak_path) / "openclaw-plugin" / "ai-resources")
    # True also when the installed kit is merely newer than the gateway (time rule), which makes
    # the restart below happen on a checkout after a top-level change, or after `brew pin`.
    stale = plugin_is_stale(plugin_dir)
    if backend_registered("agy-cli") and not stale:
        s.openclaw.gateway_restart_pending = {}   # restarted by hand since a deferral: nothing is owed
        return True

    # A plugin linked from another shell only takes effect on the next gateway start, and
    # after `brew upgrade` the running gateway holds a path from the previous version.
    if stale:
        ui.info("OpenClaw is running older plugin code than the kit on disk "
                "(`brew upgrade` moves it) — restarting the gateway.")
    try:
        restarted = restart_gateway()
    except RestartDeferred as deferred:
        _record_deferred_restart(s, deferred)
        return False
    if restarted:
        s.openclaw.gateway_restart_pending = {}
        ui.ok("OpenClaw gateway restarted; the agy-cli backend is registered.")
        return True
    ui.error("OpenClaw: the agy-cli backend is still not registered. Restart the gateway "
             "yourself with `openclaw gateway restart`, then check `openclaw plugins "
             f"inspect {PLUGIN_ID} --runtime`.")
    return False


def _configure_engine(ctx: dict, doc: dict, path: Path, ak_path: str,
                      written: list[Path]) -> bool:
    """Point the default agent at the chosen engine. True when openclaw.json changed."""
    s = ctx["state"]
    dry_run = ctx.get("dry_run", False)
    engine = ENGINES.get(s.openclaw.engine or "keep", ENGINES["keep"])

    if engine.id == "keep":
        if s.openclaw.applied and not dry_run and ui.confirm(
                "The kit configured OpenClaw's engine earlier. Restore the engine it replaced?",
                default=False):
            return _teardown_engine(s)
        ui.info("OpenClaw: engine left unchanged.")
        return False

    if engine.requires and not (s.cockpits.get(engine.requires) or state.CockpitState()).installed:
        ui.error(f"OpenClaw: engine {engine.id} needs {engine.requires}, which is not installed.")
        return False

    allowed = selection_engines(s)
    if allowed is not None and engine.id not in allowed:
        # The matrix has no verified runtime for the picked models on this engine: nothing is built or sent.
        ui.info(f"OpenClaw: skipped; no verified OpenClaw runtime for the selected models on engine {engine.id}.")
        return False

    if engine.id == "antigravity" and not s.openclaw.risk_acknowledged:
        ui.error("OpenClaw antigravity: unrestricted-execution risk was not acknowledged — "
                 "re-run the wizard interactively and accept the warning, then re-apply.")
        return False

    models = engine_models(engine, s.get_selection() if hasattr(s, "get_selection") else None)
    model = s.openclaw.model if s.openclaw.model in models else (models[0] if models else "")
    worker_model = (s.openclaw.worker_model or default_worker_model()).split("/")[-1]
    kit_skills = str(Path(ak_path) / "skills")
    engram = shutil.which("engram") or "engram"

    if not s.openclaw.applied:
        s.openclaw.previous = snapshot(doc)

    patch_kwargs: dict[str, Any] = {"openrouter_enabled": openrouter_selected(s)}
    if engine.id == "antigravity":
        # Not plain shutil.which: both official installers default to ~/.local/bin,
        # which a non-login systemd/service unit's PATH may not see. detect_agy()
        # already resolves agy this way; configure() must match it, or the absolute
        # path setup is supposed to write into the plugin config is silently "" and
        # every backend falls back to a bare `agy`/`claude` that 127s under the
        # gateway (architect decision #7).
        patch_kwargs.update(
            worker_model=worker_model,
            ak_path=ak_path,
            agy_bin=detection._which_extra("agy"),
            claude_bin=detection._which_extra("claude"),
            python3_bin=shutil.which("python3") or "",
        )
    patch = build_patch(engine, model, doc, kit_skills, engram, **patch_kwargs)

    # Switching away from antigravity to a different engine: fold in the same restore
    # fragment teardown() would apply for the antigravity-only keys (agentRuntime,
    # agents.entries, subagents.allowAgents, tools.media,
    # plugins.entries.ai-resources). Without this, those keys — plus the linked plugin
    # and the registered agy MCP bridge — stay behind forever: `engine` already points
    # at the new choice, so a later teardown() would no longer recognize them as ours.
    switching_away_from_antigravity = engine.id != "antigravity" and s.openclaw.antigravity_applied
    replace_paths: list[str] = []
    if switching_away_from_antigravity:
        frag, replace_paths = _antigravity_restore_fragment(s.openclaw.previous or {})
        _deep_merge(patch, frag)

    if engine.id == "antigravity" and not dry_run and not _register_plugin_and_backends(s, ak_path):
        return False

    ok, out = apply_patch(patch, dry_run=dry_run, replace_paths=replace_paths)
    if not ok:
        ui.error(f"OpenClaw rejected the config patch: {out[-400:]}")
        return False
    if dry_run:
        return False
    written.append(path)
    # The engine section is a deliberate wizard choice, not fill-only (ADR-0003), so say which keys it wrote.
    ui.info("changed by the engine section: " + ", ".join(openclaw_reload_rules.leaf_paths(patch)))

    if switching_away_from_antigravity:
        if s.openclaw.plugin_linked:
            rc_unlink, out_unlink = _openclaw(["plugins", "uninstall", PLUGIN_ID])
            if rc_unlink != 0:
                ui.warn(f"OpenClaw: could not unlink the ai-resources plugin ({out_unlink[-200:]})")
            s.openclaw.plugin_linked = False
        if not (s.openclaw.previous or {}).get("agy_mcp_bridge_preexisted", True):
            unregister_agy_mcp_bridge()
        # The bridge marker (if any) described a bridge that no longer exists — clear
        # it so a later switch back to antigravity registers a fresh one instead of
        # skipping registration because a stale marker is still present.
        (s.openclaw.previous or {}).pop("agy_mcp_bridge_preexisted", None)
        s.openclaw.antigravity_applied = False
        # No antigravity, no agy-cli backend to register: a deferred restart is no longer owed.
        s.openclaw.gateway_restart_pending = {}



    if engine.id == "antigravity":
        legacy_transcriber = Path.home() / ".local" / "bin" / "openclaw-transcribe"
        if legacy_transcriber.is_file() and ui.confirm(
                f"Found {legacy_transcriber}, superseded by the kit's agy-only voice "
                f"transcriber. Remove it?", default=False):
            legacy_transcriber.unlink(missing_ok=True)
            ui.ok(f"Removed {legacy_transcriber}")

    s.openclaw.applied = True
    s.openclaw.model = model
    if engine.id == "antigravity":
        s.openclaw.worker_model = worker_model
    s.openclaw.config_path = str(path)
    ui.ok(f"OpenClaw default agent → {model} via {engine.runtime}")
    return True


WORKSHOP_MODE = "propose"


def _configure_skill_workshop(s: state.SetupState, doc: dict, path: Path, written: list[Path], *,
                              dry_run: bool) -> bool:
    """Author `skills.workshop.autonomous.mode`. True when openclaw.json changed.

    A HOST-LEVEL SETTING, NOT AN ENGINE ONE — which is why it lives here and not in
    `_build_antigravity_patch`. An earlier attempt put it there and it never reached the hosts
    that need it most: `_configure_engine` returns early for `engine == "keep"`, so a host the kit
    already configured (the common case on a re-run) never rebuilt that patch.

    WHY THE KEY IS WRITTEN AT ALL. Unset means OpenClaw's default "auto", and "auto" registers a
    weekly `skill-collection-review-<agent>` job per configured agent whose toolsAllow is
    ["ls","read","write","edit","apply_patch","exec","process"] — an unsupervised turn that
    rewrites the operator's own skill files every 7 days. On a kit host it also cannot succeed:
    the review runs under `rootedExecution`, which requires the resolved CLI backend to declare
    `isolatesInstructionsWithExactTools` AND `bundleMcp`, and the kit's `claude-kit` declares
    neither on purpose. Measured on openclaw 2026.9.6: `error (2x)`,
    'CLI backend "claude-kit" does not declare instruction isolation with exact tools'. The job is
    system-owned, so `cron edit`/`cron disable` refuse it, and the monitor never auto-disables it.
    See docs/openclaw/pitfalls.md T34.

    "propose" keeps the Workshop's value — proposals still get captured for the operator to
    approve — and stops the per-agent review job from being registered at all (`workshopEnabled`
    in the monitor is `mode === "auto"`).

    ONLY WHEN UNSET. An operator who authored a mode owns that decision, including "auto" and the
    failing job that comes with it. Setup fixes the silent default, never a stated choice.
    """
    # Only on a host whose OpenClaw the kit manages. A run that configures nothing but voice, or
    # that declines every OpenClaw section, must leave openclaw.json alone — several tests exist
    # precisely to hold that contract. `applied` is set by `_configure_engine`, which runs before
    # this step, so a first antigravity/claude-code run is covered on the same pass.
    if not s.openclaw.applied:
        return False
    if _get(doc, "skills", "workshop", "autonomous", "mode") is not None:
        return False
    patch = {"skills": {"workshop": {"autonomous": {"mode": WORKSHOP_MODE}}}}
    ok, out = apply_patch(patch, dry_run=dry_run)
    if not ok:
        ui.error(f"OpenClaw rejected the Skill Workshop patch: {out[-400:]}")
        return False
    if dry_run:
        return False
    if path not in written:
        written.append(path)
    ui.ok(f"OpenClaw Skill Workshop → {WORKSHOP_MODE} (no unsupervised weekly skill rewrite)")
    return True


def _configure_mcp(s: state.SetupState, doc: dict, path: Path, written: list[Path], *,
                   dry_run: bool) -> bool:
    """Mirror Claude Code's MCP servers when the bot runs on it. True when openclaw.json changed."""
    engine = applied_engine(s) if s.openclaw.engine == "keep" else ENGINES.get(s.openclaw.engine)
    if s.openclaw.mcp != "mirror" or not engine or engine.id != "claude-code":
        if s.openclaw.mcp_mirrored and s.openclaw.mcp == "keep" and not dry_run and ui.confirm(
                "The kit mirrored Claude Code's MCP servers into OpenClaw earlier. Restore the "
                "servers it replaced?", default=False):
            return _teardown_mcp(s)
        return False
    if not mcp.configure(s, doc, _openclaw, dry_run=dry_run):
        return False
    s.openclaw.config_path = str(path)
    if path not in written:
        written.append(path)
    return True


def _teardown_mcp(s: state.SetupState) -> bool:
    """Remove or restore the MCP servers the kit mirrored. True when it finished."""
    if not s.openclaw.mcp_mirrored:
        return False
    servers = _get(read_config(config_path()), "mcp", "servers") or {}
    return mcp.teardown(s, servers, _openclaw)


def _configure_voice(s: state.SetupState, doc: dict, path: Path, ak_path: str,
                     written: list[Path], *, dry_run: bool) -> bool:
    """Write tools.media for the chosen voice mode. True when openclaw.json changed."""
    mode = s.openclaw.voice or "keep"
    if mode == "keep":
        if s.openclaw.voice_applied and not dry_run and ui.confirm(
                "The kit configured OpenClaw's voice notes earlier. Restore the setup it replaced?",
                default=False):
            return _teardown_voice(s)
        return False

    if not dry_run and mode in ("cloud", "local"):
        if not voice.ensure_local_engine(s, required=mode == "local"):
            ui.error("OpenClaw: voice notes left unchanged; the local engine is not ready.")
            return False
    # agy needs no local engine (no whisper, no ffmpeg), but every mode uses the glossary.
    if not dry_run and mode in ("agy", "cloud", "local") and voice.ensure_glossary():
        ui.ok(f"Voice glossary → {voice.transcriber.glossary_path()} (edit it freely)")

    current = _get(doc, "tools", "media")
    media = voice.build_media(mode, voice.interpreter(ak_path),
                              str(Path(ak_path) / VOICE_SCRIPT),
                              s.openclaw.voice_language or "auto", current,
                              s.openclaw.voice_correction or "llm")
    ok, out = apply_patch({"tools": {"media": media}}, dry_run=dry_run)
    if not ok:
        ui.error(f"OpenClaw rejected the voice-notes patch: {out[-400:]}")
        return False
    if dry_run:
        return False
    if not s.openclaw.voice_applied:
        s.openclaw.voice_previous = current
    s.openclaw.voice_applied = mode
    s.openclaw.config_path = str(path)
    if path not in written:
        written.append(path)
    ui.ok(f"OpenClaw voice notes → {mode}")
    return True


def _write_workspace_block(s: state.SetupState, agents_md: Path, ak_path: str,
                           gateway_url: str, *, kit: bool = False, delegates: bool = True,
                           engine_part: bool = True, dry_run: bool = False) -> bool:
    """The single writer of the kit block in a workspace AGENTS.md, one marker pair per file.

    Two parts share that pair. The engine part (`engine_part`, the orchestrator's workspace only)
    is built from what the kit has applied: CLI runtimes already load the kit block from their
    own config (CLAUDE.md, AGENTS.md, GEMINI.md), so repeating it there would inject it twice;
    only OpenClaw's own runtime needs the whole block. The memory rule goes in for every engine:
    OpenClaw injects this file into all of them, and its native memory tools would otherwise
    compete with Engram. The common part (`kit`) is the engine-independent block every agent
    workspace gets (`_shared.openclaw_agent_kit_md`); `delegates` is False only for a workspace
    that belongs to the `claude` worker alone.

    With `dry_run` nothing is written and the result says whether the file would change.
    """
    engine = applied_engine(s) if engine_part else None
    if engine and engine.id == "direct":
        md = _shared.kit_instructions_md("OpenClaw", ak_path, gateway_url, "single-model",
                                         native_skills=False) + "\n" + MEMORY_MD
    elif engine and engine.id == "antigravity":
        md = _antigravity_agents_md(ak_path)
    elif engine:
        md = f"# ai-resources (OpenClaw)\n\nEngine: {engine.label.split(' — ')[0]}.\n\n{MEMORY_MD}"
    elif engine_part and not kit:
        md = "# ai-resources (OpenClaw)\n"
    else:
        md = ""
    if kit:
        common = _shared.openclaw_agent_kit_md(ak_path, delegates=delegates)
        if md:
            # Same marker pair: drop the second H1 rather than stack two titles in one block.
            common = common.split("\n", 2)[2]
            md = md.rstrip("\n") + "\n\n" + common
        else:
            md = common

    with_voice = engine_part and s.openclaw.voice_applied in ("agy", "cloud", "local") \
        and (dry_run or _claim_voice_section(agents_md))
    if with_voice:
        md += "\n" + voice.VOICE_MD
    if not engine and not with_voice and not kit:
        if dry_run:
            return agents_md.is_file() and "BEGIN ai-resources" in agents_md.read_text(encoding="utf-8")
        return _remove_managed_block(agents_md)
    if dry_run:
        return _shared.managed_block_would_change(agents_md, md)
    return _shared.write_managed_block(agents_md, md)


def _workspace_targets(doc: dict) -> dict[Path, dict]:
    """Every workspace that gets the kit block, deduplicated by RESOLVED path.

    `~/Development` is the workspace of two agents (`claude` and `security`) and must be written
    once. `engine` marks the orchestrator's workspaces (the engine part goes there); `delegates`
    is False when the `claude` worker is the only agent that uses the path.
    """
    targets: dict[Path, dict] = {}

    def add(ws: Path | str, aid: str) -> None:
        key = Path(ws).expanduser().resolve()
        slot = targets.setdefault(key, {"aids": [], "engine": False})
        slot["aids"].append(aid)
        slot["engine"] = slot["engine"] or aid in ("main", "")

    add(workspace_dir(doc), "")   # agents.defaults.workspace: the orchestrator's home
    for aid, ws in openclaw_host.agent_workspaces(doc).items():
        for path in ws:
            add(path, aid)
    for slot in targets.values():
        slot["delegates"] = any(a not in openclaw_host.ENGINE_OWNED_ENTRIES for a in slot["aids"])
    return targets


def _configure_workspace_blocks(s: state.SetupState, doc: dict, ak_path: str, gateway_url: str,
                                written: list[Path], *, dry_run: bool, force_default: bool) -> bool:
    """Refresh the kit block in every agent workspace AGENTS.md. True when a file changed.

    Only the marked block is ever written: a file that already exists keeps every byte outside
    the markers, and what the kit created (or added a block to) is recorded for teardown.
    """
    o = s.openclaw
    default = workspace_dir(doc).resolve()
    any_changed = False
    for ws, slot in _workspace_targets(doc).items():
        if not ws.is_dir() and not (force_default and ws == default):
            # The workspace directory is the operator's to create: say which agents point at it.
            ui.warn(f"{ws}: workspace directory does not exist, so no kit block was written "
                    f"(agents: {', '.join(a or 'defaults' for a in slot['aids'])})")
            continue
        agents_md = ws / "AGENTS.md"
        if dry_run:
            if _write_workspace_block(s, agents_md, ak_path, gateway_url, kit=True,
                                      delegates=slot["delegates"], engine_part=slot["engine"],
                                      dry_run=True):
                ui.detail(f"Would refresh the kit block in {agents_md}")
            continue
        before = agents_md.read_text(encoding="utf-8") if agents_md.is_file() else None
        changed = _write_workspace_block(s, agents_md, ak_path, gateway_url, kit=True,
                                         delegates=slot["delegates"], engine_part=slot["engine"])
        after = agents_md.read_text(encoding="utf-8") if agents_md.is_file() else None
        _record_block(o, str(agents_md), before, after)
        if changed:
            any_changed = True
            if agents_md not in written:
                written.append(agents_md)
            ui.ok(f"{agents_md}: kit block refreshed")
    return any_changed


def _record_block(o: state.OpenClawState, key: str, before: str | None, after: str | None) -> None:
    """Remember how teardown must undo the kit block in `key`.

    `host_agents_md_written` (a whole file from a template) keeps its digest current, so the
    kit's own block does not turn a file nobody edited into "the user's now". `agent_kit_blocks`
    maps a path to the digest of a file the kit CREATED with only the block ("" when the file
    existed before: teardown then removes the block and nothing else).
    """
    if after is None:
        return
    digest = host_section._sha
    if key in o.host_agents_md_written:
        if before is not None and digest(before) == o.host_agents_md_written[key]:
            o.host_agents_md_written[key] = digest(after)
    elif key in o.agent_kit_blocks:
        current = o.agent_kit_blocks[key]
        if current:
            o.agent_kit_blocks[key] = digest(after) if before is not None and digest(before) == current else ""
    elif before is None:
        o.agent_kit_blocks[key] = digest(after)
    else:
        o.agent_kit_blocks[key] = ""


def _claim_voice_section(agents_md: Path) -> bool:
    """Whether the kit block may carry the voice rule.

    A hand-written `## Voice notes` section outside the block would say the same thing twice.
    Interactively the user may hand it over (a backup is kept); otherwise it stays theirs and
    the block leaves the rule out.
    """
    if not agents_md.is_file():
        return True
    raw = agents_md.read_text(encoding="utf-8")
    if not voice.hand_written_section(raw):
        return True
    if ui.is_non_interactive() or not ui.confirm(
            f"{agents_md} has its own '## Voice notes' section. Replace it with the kit-managed "
            "rule (the original file is backed up)?", default=True):
        ui.info("OpenClaw: your '## Voice notes' section is kept; the kit block leaves it out.")
        return False
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = agents_md.with_name(f"{agents_md.name}.ai-resources-backup-{stamp}")
    backup.write_text(raw, encoding="utf-8")
    agents_md.write_text(voice.remove_hand_written_section(raw), encoding="utf-8")
    ui.warn(f"{agents_md}: '## Voice notes' moved into the kit block; original saved as {backup.name}")
    return True


def _teardown_engine(s: state.SetupState) -> bool:
    """Restore the engine settings the kit replaced. True when openclaw.json changed."""
    if not s.openclaw.applied:
        return False
    prev = s.openclaw.previous or {}
    doc = read_config(config_path())
    ak_path = str(_shared.stable_kit_root(repo_root()))
    kit_skills = str(Path(ak_path) / "skills")

    patch: dict[str, Any] = {"agents": {"defaults": {
        "model": prev.get("model"),
        "models": prev.get("models"),
    }}}
    if "model_policy" in prev:
        patch["agents"]["defaults"]["modelPolicy"] = prev["model_policy"]
    if prev.get("engram") is None:
        patch["mcp"] = {"servers": {"engram": None}}
    dirs = [d for d in (_get(doc, "skills", "load", "extraDirs") or []) if d != kit_skills]
    patch["skills"] = {"load": {"extraDirs": dirs or None}}

    replace_paths = ["agents.defaults.model", "agents.defaults.models"]

    # AC-13b: build_patch toggles plugins.entries.openrouter for every engine, so
    # teardown restores it (and the untouched llama-cpp entry, snapshotted alongside
    # it) for every engine too — not only antigravity.
    patch["plugins"] = {"entries": {
        "openrouter": prev.get("openrouter_plugin"),
        "llama-cpp": prev.get("llama_cpp_plugin"),
    }}
    replace_paths += ["plugins.entries.openrouter", "plugins.entries.llama-cpp"]

    # Gated on `antigravity_applied`, not on the *current* `engine` — a run that
    # switched away from antigravity to another engine already restored these keys
    # itself (see `configure()`) and cleared the flag, so this does not double-apply;
    # a run that never switched away still needs this branch to undo them here.
    if s.openclaw.antigravity_applied:
        frag, ag_paths = _antigravity_restore_fragment(prev)
        _deep_merge(patch, frag)
        replace_paths += ag_paths

    args = ["config", "patch", "--stdin"]
    for rp in replace_paths:
        args += ["--replace-path", rp]
    rc, out = _openclaw(args, stdin=json.dumps(patch))
    if rc != 0:
        ui.error(f"OpenClaw teardown failed: {out[-400:]}")
        return False
    s.openclaw.applied = False
    s.openclaw.previous = {}
    s.openclaw.model = ""
    # The debt was for registering the plugin's backend; with the engine put back nothing owes it.
    s.openclaw.gateway_restart_pending = {}
    return True


def _teardown_voice(s: state.SetupState) -> bool:
    """Restore tools.media as it was before the kit's first voice write, then remove what
    setup installed for voice notes."""
    if not s.openclaw.voice_applied:
        return False
    prev = s.openclaw.voice_previous
    if prev is None:
        rc, out = _openclaw(["config", "patch", "--stdin"],
                            stdin=json.dumps({"tools": {"media": None}}))
    else:
        rc, out = _openclaw(["config", "patch", "--stdin", "--replace-path", "tools.media"],
                            stdin=json.dumps({"tools": {"media": prev}}))
    if rc != 0:
        ui.error(f"OpenClaw voice-notes teardown failed: {out[-400:]}")
        return False
    s.openclaw.voice_applied = ""
    s.openclaw.voice_previous = None
    voice.remove_installed(s)
    return True


def _unlink_plugin(s: state.SetupState) -> list[str]:
    """Uninstall the kit plugin when setup linked it; what was removed, for the teardown report."""
    if not s.openclaw.plugin_linked:
        return []
    rc_unlink, _out = _openclaw(["plugins", "uninstall", PLUGIN_ID])
    s.openclaw.plugin_linked = False
    return ["plugin:ai-resources"] if rc_unlink == 0 else []


def teardown(s: state.SetupState) -> list[str]:
    """Put back what the kit overwrote in openclaw.json and the workspace AGENTS.md."""
    if not (s.openclaw.applied or s.openclaw.voice_applied or s.openclaw.mcp_mirrored
            or s.openclaw.agent_kit_blocks or host_section.applied_any(s.openclaw)):
        # A deferral leaves `applied` False (the engine patch waits for the restart), so this is
        # the path that meets it: with nothing to undo there is nothing left to demand either.
        s.openclaw.gateway_restart_pending = {}
        # The same deferral leaves `plugin_linked` True while `applied` stays False, so the early
        # return used to leave the plugin linked: the kit torn down, the gateway still loading it.
        removed = _unlink_plugin(s)
        # `_register_plugin_and_backends` registers the agy bridge and records its marker BEFORE the
        # restart it then defers, so the same deferral leaves the bridge in place with `applied`
        # False. Left alone, `antigravity_applied` keeps doctor 4c warning about a plugin the kit
        # dropped, and every later teardown returns here and skips the bridge again.
        if s.openclaw.antigravity_applied:
            if not (s.openclaw.previous or {}).get("agy_mcp_bridge_preexisted", True) \
                    and unregister_agy_mcp_bridge():
                removed.append("agy-mcp:openclaw")
            (s.openclaw.previous or {}).pop("agy_mcp_bridge_preexisted", None)
            s.openclaw.antigravity_applied = False
        return removed
    doc = read_config(config_path())
    # Reverse order of configure(): the host section ran last, so it is undone first.
    host_ok = host_section.teardown(s, apply_patch=apply_patch, oc=_openclaw, doc=doc) \
        if host_section.applied_any(s.openclaw) else True
    engine_ok = _teardown_engine(s) if s.openclaw.applied else True
    mcp_ok = _teardown_mcp(s) if s.openclaw.mcp_mirrored else True
    voice_ok = _teardown_voice(s) if s.openclaw.voice_applied else True
    removed: list[str] = []
    if not (engine_ok and mcp_ok and voice_ok and host_ok):
        return removed
    removed.append(str(config_path()))

    removed += _unlink_plugin(s)

    if s.openclaw.antigravity_applied and not (s.openclaw.previous or {}).get(
            "agy_mcp_bridge_preexisted", True):
        if unregister_agy_mcp_bridge():
            removed.append("agy-mcp:openclaw")

    removed.extend(host_section.teardown_workspace_blocks(s.openclaw))
    if _remove_managed_block(workspace_dir(doc) / "AGENTS.md"):
        removed.append(str(workspace_dir(doc) / "AGENTS.md"))
    s.openclaw = state.OpenClawState()
    return removed


# --- verify (read-only) --------------------------------------------------------------------------------------

_CHAT_ID = re.compile(r"(?<![\w-])-\d{9,}\b")
_TOPIC_REF = re.compile(r"\btopic\s+(\d+)\b", re.I)


def _outside_block(text: str) -> str:
    """`text` without its managed block (all of it when there is none)."""
    lines = text.splitlines(keepends=True)
    begin, end = _shared._managed_block_span(lines)
    if begin is None or end is None:
        return text
    return "".join(lines[:begin] + lines[end + 1:])


def _verify_config_path() -> Path:
    env = os.environ.get("OPENCLAW_CONFIG_PATH")
    if env:
        return Path(env).expanduser()
    default = CONFIG_ROOT / "openclaw.json"
    return default if default.is_file() else config_path()


def _is_catch_all(entry: Any) -> bool:
    match = entry.get("match") if isinstance(entry, dict) else None
    return bool(isinstance(match, dict) and entry.get("agentId") == "main"
                and match.get("accountId") == "*" and openclaw_host._peer(entry) is None)


def _known_routing_text(doc: dict) -> tuple[set[str], str]:
    """(chat ids, everything else routed) that the live config knows about."""
    ids = {str(i) for ids in openclaw_host.group_binding_ids(doc).values() for i in ids}
    groups = (((doc.get("channels") or {}).get("telegram") or {}).get("groups")) or {}
    if isinstance(groups, dict):
        ids |= {str(k) for k in groups}
    return ids, json.dumps({"bindings": doc.get("bindings"), "groups": groups}, ensure_ascii=False)


def _verify_workspace_blocks(doc: dict, o: state.OpenClawState, F: Any) -> list:
    """E1: a recorded managed block that is now gone, or duplicated; stale routing docs outside it."""
    out: list = []
    known_ids, known_text = _known_routing_text(doc)
    good = 0
    for ws in _workspace_targets(doc):
        if not ws.is_dir():
            continue
        agents_md = ws / "AGENTS.md"
        recorded = str(agents_md) in o.agent_kit_blocks or str(agents_md) in o.host_agents_md_written
        if not agents_md.is_file():
            out.append(F("error" if recorded else "warn", "openclaw", f"{agents_md} does not exist",
                         "re-run `ai-resources setup`"))
            continue
        pairs, orphan = _shared.managed_block_pairs(agents_md)
        if pairs == 1 and not orphan:
            good += 1
        elif pairs == 0 and not orphan:
            out.append(F("error" if recorded else "warn", "openclaw",
                         f"the kit block was removed from {agents_md} after setup wrote it" if recorded
                         else f"{agents_md} has no kit block",
                         "re-run `ai-resources setup` (it rewrites only the marked block; every byte "
                         "outside the markers is kept)"))
        else:
            out.append(F("error", "openclaw", f"{agents_md} has {pairs} kit block(s)"
                         + (" and a BEGIN marker without an END" if orphan else ""),
                         "re-run `ai-resources setup` (it repairs the marked block; every byte outside "
                         "the markers is kept)"))
        outside = _outside_block(agents_md.read_text(encoding="utf-8", errors="replace"))
        stale = sorted({m for m in _CHAT_ID.findall(outside) if m not in known_ids})
        stale += sorted({f"topic {n}" for n in _TOPIC_REF.findall(outside)
                         if not re.search(rf"(?<![\w-]){n}(?![\w-])", known_text)})
        if re.search(rf"^{re.escape(_shared.OPENCLAW_LONG_RUNNING_HEADING)}[ \t]*$", outside, re.MULTILINE):
            out.append(F("warn", "openclaw",
                         f"{agents_md} repeats the kit section "
                         f"\"{_shared.OPENCLAW_LONG_RUNNING_HEADING.removeprefix('## ')}\" outside the markers",
                         "delete the hand-written copy; the kit never touches bytes outside its markers"))
        if stale:
            out.append(F("warn", "openclaw",
                         f"{agents_md} documents routing the live config does not have: {', '.join(stale)}",
                         "the kit never touches bytes outside its markers; update that text by hand"))
    if good:
        out.insert(0, F("ok", "openclaw", f"the kit block is present exactly once in {good} workspace AGENTS.md file(s)"))
    return out


def _verify_identity(doc: dict, F: Any) -> list:
    """E4: agents that share a workspace and inherit whatever name its IDENTITY.md holds."""
    entries = ((doc.get("agents") or {}).get("entries")) or {}
    by_ws: dict[Path, list[str]] = {}
    for aid, paths in openclaw_host.agent_workspaces(doc).items():
        for p in paths:
            by_ws.setdefault(p, []).append(aid)
    out: list = []
    for ws, aids in by_ws.items():
        if len(aids) < 2:
            continue
        unnamed = [a for a in aids if not (((entries.get(a) or {}).get("identity")) or {}).get("name")]
        if not unnamed:
            continue
        file_name = openclaw_host.identity_file_name(ws) or "(no IDENTITY.md name)"
        out.append(F("warn", "openclaw",
                     f"agents {', '.join(aids)} share {ws}; {ws / 'IDENTITY.md'} says Name: {file_name}, "
                     f"so {', '.join(unnamed)} (no identity.name) {'is' if len(unnamed) == 1 else 'are'} narrated with that name",
                     "; ".join(f"openclaw config set agents.entries.{a}.identity.name {a} --dry-run, inspect, then "
                               "without --dry-run" for a in unnamed) + ". The kit never writes IDENTITY.md"))
    return out


def verify(ctx: dict) -> list:
    """Read-only: what setup left in OpenClaw. Order: config checks, host, then the plugin."""
    from ...verify import Finding as F
    from . import _openclaw_host

    s = ctx["state"]
    o = s.openclaw
    out: list = []
    doc = read_config(_verify_config_path())
    if not doc:
        out.append(F("warn", ID, "openclaw.json could not be read (missing, or JSON5 the kit cannot parse); "
                     "the config checks were skipped", "run `openclaw config validate`"))
    else:
        out += _verify_workspace_blocks(doc, o, F)
        out += _verify_identity(doc, F)
        for aid, entry in (((doc.get("agents") or {}).get("entries")) or {}).items():
            raw = (entry or {}).get("model")
            if isinstance(raw, str):
                out.append(F("warn", ID, f"agent {aid} spells its model as a string ({raw}); the short form is legal "
                             "and the kit reads both",
                             f"openclaw config set agents.entries.{aid}.model.primary {raw}"))
        bindings = doc.get("bindings")
        if isinstance(bindings, list) and any(_is_catch_all(b) for b in bindings):
            if _is_catch_all(bindings[-1]):
                out.append(F("ok", ID, "the main catch-all binding is last"))
            else:
                out.append(F("error", ID, "the main catch-all binding is not last in `bindings`; it shadows the routes after it",
                             "re-run `ai-resources setup` (it keeps peer-less entries last); "
                             "never edit openclaw.json by hand"))
    host_findings = _openclaw_host.verify(ctx, runner=ctx.get("runner"))
    out += host_findings
    # The runner is threaded like the host section's above: verify must not reach the live unit.
    runner = ctx.get("runner")
    out += pending_restart_findings(
        o, main_pid=(lambda: openclaw_host.gateway_main_pid(runner)) if runner else None)
    gateway_down = any(f.level == "error" and openclaw_host.GATEWAY_UNIT in f.message for f in host_findings)
    if o.plugin_linked and not gateway_down:
        plugin = plugin_runtime()
        stable = str(Path(_shared.stable_kit_root(repo_root())) / "openclaw-plugin" / "ai-resources")
        if plugin.get("status") != "loaded":
            out.append(F("error", ID, f"the kit plugin is linked but the gateway reports it {plugin.get('status') or 'unreadable'}",
                         "`ai-resources openclaw doctor`, then restart the gateway in a maintenance window"))
        elif (why := stale_reason(stable, runner)):
            out.append(F("warn", ID, "the gateway runs the kit plugin from an older kit directory" if why == "path"
                         else "the gateway runs older kit plugin code than the kit on disk (restart to load it)",
                         "restart the gateway in a maintenance window (`ai-resources openclaw doctor` drains it safely)"))
        else:
            out.append(F("ok", ID, "the kit plugin is linked and loaded"))
    return out
