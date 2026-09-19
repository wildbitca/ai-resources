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
from . import _agy_quota
from ... import openclaw_host


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
        ("gemini-3.8-flash-low", "gemini-3.8-flash-high", "gemini-3.1-pro-low",
         "claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium"),
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
        ("anthropic/claude-sonnet-5", "anthropic/claude-opus-5", "anthropic/claude-haiku-4-5"),
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

RUNTIME_IDS = {e.runtime for e in ENGINES.values() if e.runtime}


def detect():
    return detect_openclaw()


def available_engines(s: state.SetupState) -> list[Engine]:
    """Engines whose backing CLI is installed and not permanently disabled, in preference order."""
    out = []
    for eng in ENGINES.values():
        if eng.disabled_reason:
            continue
        if eng.requires and not (s.cockpits.get(eng.requires) or state.CockpitState()).installed:
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
    if not path.is_file():
        return False
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines(keepends=True)
    begin, end = _shared._managed_block_span(lines)
    if begin is None or end is None:
        return False
    path.write_text("".join(lines[:begin] + lines[end + 1:]).lstrip("\r\n"), encoding="utf-8")
    return True


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


def _prompt_engine(s: state.SetupState, *, dry_run: bool = False) -> None:
    engines = available_engines(s)
    missing = [e for e in ENGINES.values()
              if e.requires and not e.disabled_reason and e not in engines]
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
        saved_worker = (s.openclaw.worker_model or "claude-sonnet-5").split("/")[-1]
        s.openclaw.worker_model = ui.text(
            "Model for the unrestricted claude worker agent:", default=saved_worker,
        ) or saved_worker

    if not engine.models:
        return
    saved = s.openclaw.model if s.openclaw.model in engine.models else engine.models[0]

    if engine_id == "antigravity":
        # Antigravity serves two independent weekly quota pools (see `_agy_quota.py`);
        # label each choice with its pool. `value=` stays the bare id so the patch and
        # state formats are untouched — only the label changes.
        choices = [
            ui.Choice(f"{m} — {_agy_quota.pool_for_model(m)} pool", value=m)
            for m in engine.models
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
        choices = [ui.Choice(m, value=m) for m in engine.models]

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
        if s.openclaw.model in eng.models:
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
    if dry_run or not (engine_changed or mcp_changed or voice_changed):
        return written

    agents_md = workspace_dir(doc) / "AGENTS.md"
    if _write_workspace_block(s, agents_md, ak_path, ctx.get("gateway_url", "")):
        written.append(agents_md)

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


def backend_registered(backend: str) -> bool:
    """Whether the running gateway has loaded the plugin that registers `backend`."""
    plugin = plugin_runtime()
    return plugin.get("status") == "loaded" and backend in (plugin.get("cliBackendIds") or [])


def plugin_is_stale(plugin_dir: str) -> bool:
    """Whether the gateway is running a copy of the plugin from somewhere else.

    `brew upgrade` moves the kit into a new Cellar directory, so a gateway that is still
    running keeps a path that no longer exists and answers `Unknown CLI backend` to every
    message until it restarts (seen after upgrading to 1.7.2 on 2026-09-17).
    """
    root = (plugin_runtime().get("rootDir") or "").strip()
    if not root:
        return False
    try:
        return Path(root).resolve() != Path(plugin_dir).resolve()
    except OSError:
        return root != plugin_dir


def restart_gateway(backend: str = "agy-cli") -> bool:
    """Restart the gateway and report whether the backend came back registered."""
    rc, out = _openclaw(["gateway", "restart"], timeout=180)
    if rc != 0:
        ui.warn(f"OpenClaw: `gateway restart` failed ({out[-200:]}).")
        return False
    for _ in range(BACKEND_WAIT_TRIES):
        if backend_registered(backend):
            return True
        time.sleep(BACKEND_WAIT_SECONDS)
    return False


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
    stale = plugin_is_stale(plugin_dir)
    if backend_registered("agy-cli") and not stale:
        return True

    # A plugin linked from another shell only takes effect on the next gateway start, and
    # after `brew upgrade` the running gateway holds a path from the previous version.
    if stale:
        ui.info("OpenClaw is running the plugin from an older kit directory "
                "(`brew upgrade` moves it) — restarting the gateway.")
    if restart_gateway():
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

    if engine.id == "antigravity" and not s.openclaw.risk_acknowledged:
        ui.error("OpenClaw antigravity: unrestricted-execution risk was not acknowledged — "
                 "re-run the wizard interactively and accept the warning, then re-apply.")
        return False

    model = s.openclaw.model if s.openclaw.model in engine.models else (engine.models[0] if engine.models else "")
    worker_model = (s.openclaw.worker_model or "claude-sonnet-5").split("/")[-1]
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
                           gateway_url: str) -> bool:
    """The kit block in the workspace AGENTS.md, built from what the kit has applied.

    CLI runtimes already load the kit block from their own config (CLAUDE.md, AGENTS.md,
    GEMINI.md), so repeating it here would inject it twice; only OpenClaw's own runtime needs
    the whole block. The memory rule goes in for every engine: OpenClaw injects this file into
    all of them, and its native memory tools would otherwise compete with Engram.
    """
    engine = applied_engine(s)
    if engine and engine.id == "direct":
        md = _shared.kit_instructions_md("OpenClaw", ak_path, gateway_url, "single-model",
                                         native_skills=False) + "\n" + MEMORY_MD
    elif engine and engine.id == "antigravity":
        md = _antigravity_agents_md(ak_path)
    elif engine:
        md = f"# ai-resources (OpenClaw)\n\nEngine: {engine.label.split(' — ')[0]}.\n\n{MEMORY_MD}"
    else:
        md = "# ai-resources (OpenClaw)\n"

    with_voice = s.openclaw.voice_applied in ("agy", "cloud", "local") and _claim_voice_section(agents_md)
    if with_voice:
        md += "\n" + voice.VOICE_MD
    if not engine and not with_voice:
        return _remove_managed_block(agents_md)
    return _shared.write_managed_block(agents_md, md)


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


def teardown(s: state.SetupState) -> list[str]:
    """Put back what the kit overwrote in openclaw.json and the workspace AGENTS.md."""
    if not (s.openclaw.applied or s.openclaw.voice_applied or s.openclaw.mcp_mirrored):
        return []
    doc = read_config(config_path())
    engine_ok = _teardown_engine(s) if s.openclaw.applied else True
    mcp_ok = _teardown_mcp(s) if s.openclaw.mcp_mirrored else True
    voice_ok = _teardown_voice(s) if s.openclaw.voice_applied else True
    removed: list[str] = []
    if not (engine_ok and mcp_ok and voice_ok):
        return removed
    removed.append(str(config_path()))

    if s.openclaw.plugin_linked:
        rc_unlink, _out = _openclaw(["plugins", "uninstall", PLUGIN_ID])
        if rc_unlink == 0:
            removed.append("plugin:ai-resources")
        s.openclaw.plugin_linked = False

    if s.openclaw.antigravity_applied and not (s.openclaw.previous or {}).get(
            "agy_mcp_bridge_preexisted", True):
        if unregister_agy_mcp_bridge():
            removed.append("agy-mcp:openclaw")

    if _remove_managed_block(workspace_dir(doc) / "AGENTS.md"):
        removed.append(str(workspace_dir(doc) / "AGENTS.md"))
    s.openclaw = state.OpenClawState()
    return removed
