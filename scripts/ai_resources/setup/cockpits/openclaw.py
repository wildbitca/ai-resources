"""OpenClaw cockpit configurator — point a chat-facing agent at the rest of the kit.

OpenClaw is not a coding CLI: it fronts chat channels (Telegram, Slack, …) and
hands every turn to an *agent runtime*. The runtime decides how much of the kit
the bot can use, so this module's one real decision is which engine runs it:

    claude-code  runtime `claude-cli`: every turn runs Claude Code with the user's
                 ~/.claude settings, so the bot gets the kit's subagents, skills,
                 hooks, workflow scripts and CLAUDE.md block exactly as the
                 Claude Code cockpit installed them.
    codex        runtime `codex`: turns run through the Codex harness, which reads
                 the Codex cockpit's ~/.codex setup.
    gemini-cli   runtime `google-gemini-cli`: turns run through Gemini CLI, which
                 reads the Gemini cockpit's ~/.gemini setup.
    direct       runtime `openclaw`: OpenClaw calls a model provider itself
                 (OpenRouter by default). No coding CLI sits in between, so this
                 module supplies the kit directly: skills via
                 `skills.load.extraDirs` and the instructions block in the
                 workspace AGENTS.md, which OpenClaw injects into every turn.
    keep         change nothing.

Verified against OpenClaw 2026.9.4. OpenClaw starts CLI runtimes with
`--strict-mcp-config`, so MCP servers from the CLI's own config never reach the
bot; Engram is therefore always declared in OpenClaw's `mcp.servers`.

Writes go through `openclaw config patch --stdin`, OpenClaw's validated writer,
never straight to openclaw.json: the gateway journals and fingerprints that file.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import state, ui
from ..detection import detect_openclaw
from ... import repo_root
from . import _shared


NAME = "OpenClaw"
ID = "openclaw"
CONFIG_ROOT = Path.home() / ".openclaw"


@dataclass(frozen=True)
class Engine:
    id: str
    label: str
    runtime: str            # OpenClaw agentRuntime.id; "" for keep
    requires: str           # cockpit id that must be installed; "" for none
    models: tuple[str, ...]  # OpenClaw model refs, first is the default


ENGINES: dict[str, Engine] = {
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
    ),
    "direct": Engine(
        "direct", "Direct model (OpenRouter) — OpenClaw's own runtime, kit skills + instructions only",
        "openclaw", "",
        ("openrouter/anthropic/claude-haiku-4.5", "openrouter/anthropic/claude-sonnet-5",
         "openrouter/auto"),
    ),
    "keep": Engine("keep", "Keep OpenClaw's current engine — change nothing", "", "", ()),
}


def detect():
    return detect_openclaw()


def available_engines(s: state.SetupState) -> list[Engine]:
    """Engines whose backing CLI is installed, in preference order."""
    out = []
    for eng in ENGINES.values():
        if eng.requires and not (s.cockpits.get(eng.requires) or state.CockpitState()).installed:
            continue
        out.append(eng)
    return out


def default_engine(s: state.SetupState) -> str:
    """The saved answer, else Claude Code when it is installed, else keep."""
    ids = [e.id for e in available_engines(s)]
    if s.openclaw.engine in ids:
        return s.openclaw.engine
    if ui.is_non_interactive():
        # Never repoint a live bot from a first unattended run.
        return "keep"
    return "claude-code" if "claude-code" in ids else "keep"


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
    """What the kit is about to overwrite, so teardown can put it back exactly."""
    return {
        "model": _get(doc, "agents", "defaults", "model"),
        "models": _get(doc, "agents", "defaults", "models"),
        "engram": _get(doc, "mcp", "servers", "engram"),
        "extra_dirs": _get(doc, "skills", "load", "extraDirs"),
        "model_policy": _get(doc, "agents", "defaults", "modelPolicy"),
    }


def _allowed(model: str, allow: list) -> bool:
    return any(a == model or (isinstance(a, str) and a.endswith("*") and model.startswith(a[:-1]))
               for a in allow)


def build_patch(engine: Engine, model: str, doc: dict, kit_skills: str,
                engram_command: str) -> dict:
    """The openclaw.json patch that points the default agent at `engine`."""
    patch: dict[str, Any] = {
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
        if rid in {e.runtime for e in ENGINES.values() if e.runtime} and rid != engine.runtime:
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

    dirs = [d for d in (_get(doc, "skills", "load", "extraDirs") or []) if d != kit_skills]
    if engine.id == "direct":
        dirs.append(kit_skills)
    if dirs != (_get(doc, "skills", "load", "extraDirs") or []):
        patch["skills"] = {"load": {"extraDirs": dirs}}
    return patch


def apply_patch(patch: dict, *, dry_run: bool = False) -> tuple[bool, str]:
    args = ["config", "patch", "--stdin"] + (["--dry-run"] if dry_run else [])
    rc, out = _openclaw(args, stdin=json.dumps(patch))
    return rc == 0, out


MEMORY_MD = """## Memory

- **Engram is the memory of record.** Before answering anything that may depend on earlier work,
  call `mem_search`; after a decision, a fix, a discovery or a stated preference, call `mem_save`.
  Engram is shared with every other cockpit on this machine, so what the bot saves here is what
  Claude Code, Codex or Gemini CLI recall later, and the other way round.
- OpenClaw's own memory (MEMORY.md, memory files, transcript recall) is recent chat context only.
  Do not treat it as the durable store, and do not copy Engram content into it.
"""


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


# --- setup entry points ------------------------------------------------------------

def prompt(s: state.SetupState) -> None:
    """Ask which engine runs the bot and which model it uses. Called from step 7."""
    engines = available_engines(s)
    missing = [e for e in ENGINES.values() if e.requires and e not in engines]
    if missing:
        ui.detail("Not offered (CLI not installed): " + ", ".join(e.id for e in missing))
    engine_id = ui.select(
        "Which engine should OpenClaw run its agents on?",
        [ui.Choice(e.label, value=e.id) for e in engines],
        default=default_engine(s),
    )
    s.openclaw.engine = engine_id
    engine = ENGINES[engine_id]
    if not engine.models:
        return
    saved = s.openclaw.model if s.openclaw.model in engine.models else engine.models[0]
    s.openclaw.model = ui.select(
        "Model for OpenClaw's default agent:",
        [ui.Choice(m, value=m) for m in engine.models],
        default=saved,
    )
    # OpenClaw hands Claude Code the bare Anthropic ID (claude-sonnet-5) whatever the
    # ref says. That works under either gateway: LiteLLM registers bare Claude IDs,
    # and under OpenRouter the Claude Code cockpit maps them with `modelOverrides`.


def configure(ctx: dict) -> list[Path]:
    s = ctx["state"]
    dry_run = ctx.get("dry_run", False)
    written: list[Path] = []

    cs = s.cockpits.get(ID) or state.CockpitState()
    cs.installed = True
    cs.version = ctx.get("detected_version", cs.version)
    cs.binary_path = ctx.get("detected_path", cs.binary_path)
    cs.config_root = str(CONFIG_ROOT)

    engine = ENGINES.get(s.openclaw.engine or "keep", ENGINES["keep"])
    path = config_path()
    doc = read_config(path)

    if engine.id == "keep":
        if s.openclaw.applied and not dry_run and ui.confirm(
                "The kit configured OpenClaw's engine earlier. Restore the engine it replaced?",
                default=False):
            return [Path(p) for p in teardown(s)]
        ui.info("OpenClaw: engine left unchanged.")
        return written

    if engine.requires and not (s.cockpits.get(engine.requires) or state.CockpitState()).installed:
        ui.error(f"OpenClaw: engine {engine.id} needs {engine.requires}, which is not installed.")
        return written

    model = s.openclaw.model if s.openclaw.model in engine.models else engine.models[0]
    ak_path = str(_shared.stable_kit_root(repo_root()))
    kit_skills = str(Path(ak_path) / "skills")
    engram = shutil.which("engram") or "engram"

    if not s.openclaw.applied:
        s.openclaw.previous = snapshot(doc)

    patch = build_patch(engine, model, doc, kit_skills, engram)
    ok, out = apply_patch(patch, dry_run=dry_run)
    if not ok:
        ui.error(f"OpenClaw rejected the config patch: {out[-400:]}")
        return written
    if dry_run:
        return written
    written.append(path)

    # Instructions: CLI runtimes already load the kit block from their own config
    # (CLAUDE.md, AGENTS.md, GEMINI.md), so repeating it here would inject it twice;
    # only OpenClaw's own runtime needs the whole block. The memory rule goes in for
    # every engine: OpenClaw injects this file into all of them, and its native
    # memory tools would otherwise compete with Engram.
    agents_md = workspace_dir(doc) / "AGENTS.md"
    if engine.id == "direct":
        md = _shared.kit_instructions_md("OpenClaw", ak_path, ctx.get("gateway_url", ""), "single-model",
                                         native_skills=False) + "\n" + MEMORY_MD
    else:
        md = f"# ai-resources (OpenClaw)\n\nEngine: {engine.label.split(' — ')[0]}.\n\n{MEMORY_MD}"
    if _shared.write_managed_block(agents_md, md):
        written.append(agents_md)

    s.openclaw.applied = True
    s.openclaw.model = model
    s.openclaw.config_path = str(path)
    cs.configured = True
    cs.last_configured_at = datetime.now(timezone.utc).isoformat()
    s.cockpits[ID] = cs
    ui.ok(f"OpenClaw default agent → {model} via {engine.runtime}")
    return written


def teardown(s: state.SetupState) -> list[str]:
    """Put back what the kit overwrote in openclaw.json and the workspace AGENTS.md."""
    if not s.openclaw.applied:
        return []
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

    args = ["config", "patch", "--stdin",
            "--replace-path", "agents.defaults.model", "--replace-path", "agents.defaults.models"]
    rc, out = _openclaw(args, stdin=json.dumps(patch))
    removed: list[str] = []
    if rc != 0:
        ui.error(f"OpenClaw teardown failed: {out[-400:]}")
        return removed
    removed.append(str(config_path()))
    if _remove_managed_block(workspace_dir(doc) / "AGENTS.md"):
        removed.append(str(workspace_dir(doc) / "AGENTS.md"))
    s.openclaw = state.OpenClawState()
    return removed
