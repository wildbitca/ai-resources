"""Claude Code cockpit configurator — full multi-model support."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import state, ui
from ..detection import detect_claude_code
from ... import repo_root
from ... import domains, skill_scope
from .. import role_render
from .. import effort as effort_mod
from . import _shared
from .. import cockpit_auth
from .. import live_auth


NAME = "Claude Code"
ID = "claude"
CONFIG_ROOT = Path.home() / ".claude"
SETTINGS_PATH = CONFIG_ROOT / "settings.json"
CLAUDE_MD_PATH = CONFIG_ROOT / "CLAUDE.md"
AGENTS_DIR = CONFIG_ROOT / "agents"
WORKFLOWS_DIR = CONFIG_ROOT / "workflows"

# Env keys only meaningful in multi-model mode — must be removed on teardown or
# when re-configuring in single-model mode, even if they were pre-existing at
# the time of the last multi-model setup (and therefore not in the tracking record).
#
# The openrouter backend writes more than the LiteLLM one: a bearer credential,
# a deliberately blank ANTHROPIC_API_KEY, and the class-level model overrides.
# Leaving any of them behind on teardown would keep Claude Code pointed away
# from the user's own subscription, so every key this cockpit can write is
# listed here — removal is a no-op for the ones absent from settings.json.
#
# ANTHROPIC_API_KEY is deliberately NOT in this list. It is the one key here
# that has a legitimate meaning the kit never gave it: the user's own API key,
# which the LiteLLM backend never writes. Listing it unconditionally marked that
# key as ours and deleted it on teardown. It is reclaimed only through
# `newly_added` below, i.e. only when this run actually introduced it.
_MULTI_MODEL_ONLY_ENV_KEYS: list[str] = [
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
]


# Tools each role may use — used when generating subagent files.
ROLE_TOOLS: dict[str, str | None] = {
    "explore":            "Read, Grep, Glob",
    # Write, because the role is told to update the handoff and had no tool for
    # it: with Bash denied it went mute and the orchestrator transcribed for it.
    # Sequential step, so there is no concurrent-writer hazard.
    "planner":            "Read, Edit, Write, Grep, Glob, Bash",
    "implementer":        "Read, Edit, Write, Bash, Grep, Glob",
    "tester":             "Read, Edit, Write, Bash, Grep, Glob",
    "code-reviewer":      "Read, Bash, Grep, Glob",
    "security-auditor":   "Read, Bash, Grep, Glob",
    # Same reason as planner: instructed to record Architecture_design_ref and
    # Architect_approval in the handoff, with no way to write them.
    "software-architect": "Read, Edit, Write, Grep, Glob, Bash",
    "verifier":           "Read, Bash, Grep, Glob",
    "doc-writer":         "Read, Edit, Write, Bash, Grep, Glob",
    # Specialists get full tool access (None = inherit all)
    "generalPurpose":              None,
    "crashlytics-fixer":           None,
    "sentry-fixer":                None,
    "package-upgrade":             None,
    "terraform-maintainer":        None,
    "crossplane-upjet-maintainer": None,
}


def detect():
    return detect_claude_code()


NATIVE_MODELS = ("inherit", "opus", "sonnet", "haiku", "fable", "opusplan")
KIT_HOOK_SCRIPTS = ("kit_session_start.py", "kit_subagent_return.py", "kit_handoff_guard.py", "agy_evidence_guard.py",
                    "kit_sleep_guard.py")
# Opt-in hooks that only make sense on a host running OpenClaw. They are installed by the
# OpenClaw cockpit's host section, never by the always-on kit merge below, so a machine
# without OpenClaw never gets them (`hooks/hooks.json` still lists them for the plugin route,
# where both are inert: the team hook needs OPENCLAW_CLI=1 and the guard needs a live gateway).
OPENCLAW_HOOK_SCRIPTS = ("openclaw_team_progress.py", "openclaw_gateway_guard.py")
# Where the team hook lived before the kit shipped it. A registration pointing there is a
# hand-installed copy of the same hook: leaving it beside the kit entry publishes every team
# event to Telegram twice.
LEGACY_OPENCLAW_HOOK_MARKERS = (".claude/hooks/openclaw-team-progress.py",)


def _is_native_model(model: str) -> bool:
    """True for values Claude Code resolves without a gateway."""
    return model in NATIVE_MODELS or model.startswith("claude-")


def _is_kit_hook_command(command: str) -> bool:
    return any(f"/hooks/{script}" in command for script in KIT_HOOK_SCRIPTS)


def _is_openclaw_hook_command(command: str) -> bool:
    """A registration of one of the kit's OpenClaw hooks (not the legacy hand-installed copy)."""
    return any(f"/hooks/{script}" in command for script in OPENCLAW_HOOK_SCRIPTS)


def _is_legacy_openclaw_hook_command(command: str) -> bool:
    return any(marker in command for marker in LEGACY_OPENCLAW_HOOK_MARKERS)


def _kit_hooks(ak_path: str) -> dict[str, list[dict]]:
    """Hook entries the kit installs in ~/.claude/settings.json (scripts live in <kit>/hooks/).

    `python3` is resolved at hook run time: a path frozen at setup (a venv or pyenv shim)
    could disappear and break every tool call.
    """

    def entry(script: str, matcher: str = "") -> dict:
        hook = {"type": "command", "command": f'python3 "{ak_path}/hooks/{script}"', "timeout": 10}
        return {"matcher": matcher, "hooks": [hook]} if matcher else {"hooks": [hook]}

    return {
        "SessionStart": [entry("kit_session_start.py")],
        "SubagentStop": [entry("kit_subagent_return.py")],
        "PreToolUse": [entry("kit_handoff_guard.py", "Write|Edit|MultiEdit|NotebookEdit|Bash"),
                       # ADR-0010 PD-7: no agent session writes the Antigravity gate evidence record.
                       entry("agy_evidence_guard.py", "Write|Edit|MultiEdit|NotebookEdit|Bash"),
                       # ADR-0014 D7: a Bash `sleep` longer than 30 s is refused (polling is the costliest wait).
                       entry("kit_sleep_guard.py", "Bash")],
    }


def _openclaw_hooks(ak_path: str, *, team: bool = True, guard: bool = True) -> dict[str, list[dict]]:
    """Entries for the OpenClaw host hooks. Timeouts are the ones measured on the live host:
    the team hook spawns detached processes and the per-tool call needs the longer budget."""

    def entry(script: str, matcher: str = "", timeout: int = 10) -> dict:
        hook = {"type": "command", "command": f'python3 "{ak_path}/hooks/{script}"', "timeout": timeout}
        return {"matcher": matcher, "hooks": [hook]} if matcher else {"hooks": [hook]}

    out: dict[str, list[dict]] = {}
    if team:
        out["PreToolUse"] = [entry("openclaw_team_progress.py", "*", 30)]
        out["SubagentStop"] = [entry("openclaw_team_progress.py", "", 30)]
        out["UserPromptSubmit"] = [entry("openclaw_team_progress.py", "", 20)]
        out["Stop"] = [entry("openclaw_team_progress.py", "", 20)]
    if guard:
        # Bash only: the guard has nothing to say about an edit, and a narrower matcher means it
        # is not spawned for every Write/Edit on the host.
        out.setdefault("PreToolUse", []).append(entry("openclaw_gateway_guard.py", "Bash"))
    return out


def legacy_openclaw_entries(settings_path: Path | None = None) -> list[dict]:
    """The hand-installed legacy team-hook registrations in settings.json, as
    [{"event": ..., "entry": {...}}] where each entry carries ONLY the legacy hook(s).

    Read-only. Recorded before `install_openclaw_hooks` replaces them so teardown can put them back.
    """
    path = settings_path or SETTINGS_PATH
    try:
        hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks", {})
    except (OSError, ValueError, AttributeError):
        return []
    found: list[dict] = []
    for event, entries in (hooks.items() if isinstance(hooks, dict) else []):
        for entry in entries if isinstance(entries, list) else []:
            if not (isinstance(entry, dict) and isinstance(entry.get("hooks"), list)):
                continue
            legacy = [h for h in entry["hooks"]
                      if isinstance(h, dict) and _is_legacy_openclaw_hook_command(str(h.get("command", "")))]
            if legacy:
                found.append({"event": event, "entry": {**entry, "hooks": legacy}})
    return found


def restore_legacy_openclaw_hooks(recorded: list[dict], settings_path: Path | None = None) -> bool:
    """Put back the registrations `legacy_openclaw_entries` recorded. Idempotent: an entry whose
    command is already registered under that event is not added twice. True when the file changed.
    The entry goes at the end of its event's list (its original position is not recorded)."""
    if not recorded:
        return False
    path = settings_path or SETTINGS_PATH
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        ui.warn(f"{path} is not valid JSON: the legacy OpenClaw hook was not restored.")
        return False
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict):
        return False
    hooks = settings.setdefault("hooks", {})
    changed = False
    for item in recorded:
        event, entry = item.get("event"), item.get("entry")
        if not (isinstance(event, str) and isinstance(entry, dict)):
            continue
        current = hooks.setdefault(event, [])
        if not isinstance(current, list):
            continue
        present = {h.get("command") for e in current if isinstance(e, dict)
                   for h in (e.get("hooks") or []) if isinstance(h, dict)}
        if all(h.get("command") in present for h in entry.get("hooks", [])):
            continue
        current.append(entry)
        changed = True
    if not changed:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def install_openclaw_hooks(ak_path: str, *, team: bool, guard: bool,
                           settings_path: Path | None = None) -> bool:
    """Make ~/.claude/settings.json carry exactly the requested OpenClaw hooks.

    Earlier kit entries are replaced (so a second run is a byte-level no-op) and, when the team
    hook is wanted, the hand-installed legacy copy is replaced too. The user's own hooks stay.
    The legacy entries are NOT recorded here: call `legacy_openclaw_entries` first and hand the
    result to `restore_legacy_openclaw_hooks` on teardown, which is what puts them back.
    Returns True when the file changed.
    """
    def ours(command: str) -> bool:
        return _is_openclaw_hook_command(command) or (team and _is_legacy_openclaw_hook_command(command))

    return _shared.merge_kit_hooks(settings_path or SETTINGS_PATH,
                                   _openclaw_hooks(ak_path, team=team, guard=guard), ours)


def remove_openclaw_hooks(settings_path: Path | None = None) -> bool:
    """Undo `install_openclaw_hooks` for the kit's own entries. It does not restore the legacy
    hand-installed copy that install replaced: `restore_legacy_openclaw_hooks` does, from the
    entries the caller recorded."""
    return _shared.merge_kit_hooks(settings_path or SETTINGS_PATH, {}, _is_openclaw_hook_command)


def _cleanup_stale_hooks(settings: dict) -> bool:
    """Remove the workflow-enforcement prompt hook installed by kit v0.7.0 (removed in v0.7.1).

    Only prompt-type UserPromptSubmit entries about workflows are removed; user hooks stay.
    """
    hooks = settings.get("hooks")
    entries = hooks.get("UserPromptSubmit") if isinstance(hooks, dict) else None
    if not isinstance(entries, list):
        return False

    def legacy(entry: Any) -> bool:
        return isinstance(entry, dict) and any(
            isinstance(h, dict) and h.get("type") == "prompt" and "workflow" in str(h.get("prompt", "")).lower()
            for h in entry.get("hooks") or []
        )

    kept = [e for e in entries if not legacy(e)]
    if len(kept) == len(entries):
        return False
    if kept:
        hooks["UserPromptSubmit"] = kept
    else:
        del hooks["UserPromptSubmit"]
    return True


def _openrouter_model_overrides() -> dict[str, str]:
    """Bare Anthropic model ID -> OpenRouter catalogue ID, for `modelOverrides`.

    Claude Code sends a full model ID it is handed through `--model` verbatim, and
    OpenRouter only knows namespaced ones: `claude --model claude-sonnet-5` fails
    under the openrouter backend even though every alias works. That is exactly
    how OpenClaw launches Claude Code — it normalises its model ref to the bare
    Anthropic ID — so without this map a chat bot on the claude-cli runtime loses
    its main conversation the moment setup switches to OpenRouter. `modelOverrides`
    applies to `--model` from Claude Code 2.1.200. The catalogue spells versions
    with dots (claude-haiku-4.5); Anthropic's own IDs use dashes (claude-haiku-4-5).
    """
    from ..providers import KNOWN_MODELS
    return {
        ref.split("/", 1)[1].replace(".", "-"): ref
        for ref in KNOWN_MODELS.get("openrouter", [])
        if ref.startswith("anthropic/")
    }


# Override maps written by earlier kit generations. Removal still recognises
# them (exact key and value) so they are not left behind as user entries.
_PREVIOUS_KIT_MODEL_OVERRIDES: tuple[dict[str, str], ...] = (
    {
        "claude-opus-5": "anthropic/claude-opus-5",
        "claude-sonnet-5": "anthropic/claude-sonnet-5",
        "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
        "claude-fable-5-1": "anthropic/claude-fable-5.1",
    },
)


def _remove_kit_model_overrides(path: Path) -> list[str]:
    """Drop the `modelOverrides` entries this cockpit writes, keeping the user's own.

    An entry counts as the kit's only when both its key and value match the map
    above, so a user who pointed claude-opus-5 somewhere else keeps that mapping.
    """
    if not path.is_file():
        return []
    try:
        cur = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    overrides = cur.get("modelOverrides")
    if not isinstance(overrides, dict):
        return []
    maps = (_openrouter_model_overrides(), *_PREVIOUS_KIT_MODEL_OVERRIDES)
    removed = [k for k, v in overrides.items() if any(m.get(k) == v for m in maps)]
    if not removed:
        return []
    for k in removed:
        overrides.pop(k)
    if not overrides:
        cur.pop("modelOverrides")
    path.write_text(json.dumps(cur, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return removed


# Lean-cost env (fill-only: an operator value is kept). Verified on Claude Code 2.1.308: the binary
# reads CLAUDE_CODE_AUTO_COMPACT_WINDOW (it takes precedence over the `autoCompactWindow` setting).
# A 1M-context session otherwise compacts only near 1M tokens; 200k keeps every call under the
# long-prompt cost tier (2026-10-22 baseline: 39.6% of calls were above 200k).
COST_ENV_FILL_ONLY: dict[str, str] = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}

# One-time migration: the kit used to write CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 and no kit workflow
# uses agent teams (rules/014 only names the upstream agent-teams-lite docs). Only the kit-written value
# "1" is removed; any other value is the operator's own and stays.
AGENT_TEAMS_MIGRATION_ID = "claude-env-drop-agent-teams-v1"
AGENT_TEAMS_KEY = "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"


def _apply_cost_env(patch: dict, existing: dict) -> list[str]:
    """Add the fill-only cost env keys the operator has not set. Returns the keys added to the patch."""
    have = existing.get("env") if isinstance(existing.get("env"), dict) else {}
    added = []
    for key, value in COST_ENV_FILL_ONLY.items():
        if key not in have:
            patch.setdefault("env", {})[key] = value
            added.append(key)
    return added


def _migrate_agent_teams(path: Path, applied: list[str]) -> bool:
    """Remove the kit-written AGENT_TEAMS=1 once; record the migration id. Returns True when removed."""
    if AGENT_TEAMS_MIGRATION_ID in applied:
        return False
    removed = False
    if path.is_file():
        try:
            cur = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        env = cur.get("env") if isinstance(cur, dict) else None
        if isinstance(env, dict) and env.get(AGENT_TEAMS_KEY) == "1":
            _shared.remove_env_keys_from_settings(path, [AGENT_TEAMS_KEY])
            removed = True
    applied.append(AGENT_TEAMS_MIGRATION_ID)
    return removed


VERTEX_ENV_KEYS = ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")
AUTH_PROMPT = "Claude Code sign-in:"
CHANGE_PROMPT = "How should Claude Code sign in?"
HELPER_PROMPT = "apiKeyHelper: a command that prints the key, or the path of a file only you can read"
KEEP, CHANGE = "keep", "change"
LOGIN, HELPER, VERTEX_ADC = "login", "api_key_helper", "vertex_adc"
LOGIN_ARGV = ["claude", "auth", "login"]
HELPER_OK_ATTR = "_claude_helper_confirmed"
"""Instance attribute on the setup state (never serialised): the operator chose this apiKeyHelper in this run."""
VERTEX_PENDING_ATTR = "_claude_vertex_pending"
"""Instance attribute on the setup state (never serialised): project and region typed in this run, written at Apply."""

# live kind -> the legacy method name the state keeps as a fill-only default
_METHOD_OF_KIND = {live_auth.SUBSCRIPTION: "subscription", live_auth.API_KEY: "api_key",
                   live_auth.API_KEY_HELPER: "api_key", live_auth.VERTEX: "vertex_adc"}


def change_options() -> list[tuple[str, str, str]]:
    """(value, label, one-line effect) of the "change" question (ADR-0016 D1). Claude Code owns its sign-in; the only
    kit extra is the apiKeyHelper setting, because Claude Code has no flow that stores a key for you."""
    return [
        (LOGIN, "Sign in with Claude Code's own flow",
         "hands this terminal to `claude auth login`; the kit never sees the credential"),
        (HELPER, "Use an API key helper (apiKeyHelper)",
         "the kit writes only the command or file path into settings.json and never reads or stores the key"),
        (VERTEX_ADC, "Google Vertex AI",
         "your Application Default Credentials; only the project and region are kept"),
    ]


def read_live(runner=None) -> "live_auth.LiveAuth":
    """Claude Code's sign-in as Claude Code itself reports it (env, settings, `claude auth status`). Read-only."""
    return live_auth.read_claude(runner, os.environ, live_auth.read_claude_settings(SETTINGS_PATH))


def helper_setting(value: str) -> tuple[str, str]:
    """`(apiKeyHelper setting, problem)` for what the operator typed. A command is kept as typed. A path to a file must
    be yours with no group or other access (0600, or 0700 for a script that prints the key): a readable key file is
    refused. A key pasted by mistake is refused too, because the kit stores a path or command, never a key."""
    import shlex
    import stat as _stat
    text = (value or "").strip()
    if not text:
        return "", "nothing was entered"
    if cockpit_auth.looks_like_secret(text):
        return "", "that looks like a key itself; give a command or a file path, the kit never stores a key"
    expanded = os.path.expanduser(text)
    if expanded.startswith("/"):
        try:
            st = os.stat(expanded)
        except OSError:
            return "", f"{text} does not exist"
        if not _stat.S_ISREG(st.st_mode):
            return "", f"{text} is not a regular file"
        if st.st_uid != os.getuid():
            return "", f"{text} is not owned by you"
        executable = bool(st.st_mode & 0o100)
        if st.st_mode & 0o077 or (not executable and st.st_mode & 0o777 != 0o600):
            return "", (f"{text} has mode {st.st_mode & 0o777:04o}; a key file must be 0600 "
                        f"(chmod 600 {shlex.quote(text)})")
        return (expanded if executable else f"cat {shlex.quote(expanded)}"), ""
    return text, ""


def record_live(s: Any, auth: "live_auth.LiveAuth") -> None:
    """Record what Claude Code REPORTS, not what was answered. "API key" can only appear when a key or helper is live;
    a saved `api_key` with no live evidence falls back to the subscription method (a harmless default, no key)."""
    cockpit_auth.set_live(s, ID, auth.kind)
    method = _METHOD_OF_KIND.get(auth.kind)
    if method is None:
        saved = cockpit_auth.method(s, ID)
        method = saved if saved in ("subscription", "vertex_adc") else "subscription"
    cockpit_auth.set_method(s, ID, method)


def ask_auth(ui_mod: Any, s: Any, detected: Any = None, dry_run: bool = False) -> int:
    """Show Claude Code's LIVE sign-in and ask keep or change (ADR-0016 D1). 0 on an answer, 130 on a cancel.

    Change hands the terminal to Claude Code's own flow on an explicit choice only, never in a non-interactive run or a
    dry run. The kit stores a kind name, a helper path or command and Vertex's project and region: never a key."""
    auth = read_live()
    ui_mod.info(f"Claude Code is signed in with: {auth.line()}" if live_auth.claude_signed_in(auth)
                else f"Claude Code sign-in: {auth.label}")
    if ui_mod.is_non_interactive():
        record_live(s, auth)
        return 0
    picked = ui_mod.select(AUTH_PROMPT, [ui_mod.Choice("Keep it", value=KEEP),
                                         ui_mod.Choice("Change it", value=CHANGE)], default=KEEP)
    if picked is None:
        return 130
    if picked == CHANGE:
        how = ui_mod.select(CHANGE_PROMPT, [ui_mod.Choice(f"{label} - {line}", value=key)
                                            for key, label, line in change_options()], default=LOGIN)
        if how is None:
            return 130
        auth = _change(ui_mod, s, how, dry_run=dry_run) or read_live()
        record_live(s, auth)
        if how == VERTEX_ADC and not dry_run:
            # The answer only drives what Apply writes (the legacy fill-only method that adds the Vertex env). The
            # recorded sign-in stays what Claude Code reports after the re-read, never the answer (plan S7, ADR-0016).
            cockpit_auth.set_method(s, ID, "vertex_adc")
        return 0
    record_live(s, auth)
    return 0


def _change(ui_mod: Any, s: Any, how: str, *, dry_run: bool) -> "live_auth.LiveAuth | None":
    """Carry out the chosen change. Returns the live state to record, or None to re-read it."""
    from .. import credentials
    if how == LOGIN:
        shown = " ".join(LOGIN_ARGV)
        if dry_run:
            ui_mod.detail(f"Dry run: would hand this terminal to `{shown}`.")
            return None
        ui_mod.info(f"Applies now: `{shown}` runs on this terminal; the kit sees nothing you type.")
        rc = live_auth.run_handoff(LOGIN_ARGV)
        if rc != 0:
            ui_mod.warn(f"`{shown}` exited {rc}; the sign-in is whatever Claude Code reports now.")
        else:
            ui_mod.record_action("signed in with Claude Code's own flow")       # Back stops here: this ran for real
        return None                      # re-read: record the live kind, not the answer
    if how == HELPER:
        typed = ui_mod.text(HELPER_PROMPT, default=cockpit_auth.helper(s, ID))
        setting, problem = helper_setting(typed or "")
        if problem:
            ui_mod.warn(f"apiKeyHelper not set: {problem}.")
            return None
        cockpit_auth.set_helper(s, ID, setting)
        setattr(s, HELPER_OK_ATTR, True)
        ui_mod.detail("apiKeyHelper is written to settings.json at Apply; the kit never reads or stores the key.")
        return live_auth.LiveAuth(live_auth.API_KEY_HELPER, live_auth.B_API, "Anthropic API key (apiKeyHelper)")
    if how == VERTEX_ADC:
        updates: dict[str, str] = {}
        project = ui_mod.text("GOOGLE_CLOUD_PROJECT (GCP project ID)",
                              default=credentials.get_key("GOOGLE_CLOUD_PROJECT")
                              or os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
        if project:
            updates["GOOGLE_CLOUD_PROJECT"] = project
        # "global": regional endpoints serve older Sonnet models only (ADR-0008 addendum), so it is the default.
        location = ui_mod.text("GOOGLE_CLOUD_LOCATION (global, us, eu or a region)",
                               default=credentials.get_key("GOOGLE_CLOUD_LOCATION") or "global")
        if location:
            updates["GOOGLE_CLOUD_LOCATION"] = location
        if updates and not dry_run:
            setattr(s, VERTEX_PENDING_ATTR, updates)          # written at Apply (configure), never here
        ui_mod.detail("Vertex AI uses your Application Default Credentials (`gcloud auth application-default login`).")
        return None
    return None


def _apply_pending_vertex(s: Any) -> None:
    """Apply-time half of the Vertex answer: the project and region go to the kit .env, tracked for teardown."""
    from .. import credentials
    updates = getattr(s, VERTEX_PENDING_ATTR, None)
    if not updates:
        return
    _, added = credentials.update_env_tracked(updates)
    for key in added:
        if key not in s.tracking.env_keys_added:
            s.tracking.env_keys_added.append(key)
    ui.record_action("saved the Vertex project and region")
    setattr(s, VERTEX_PENDING_ATTR, None)


def _vertex_env() -> dict[str, str]:
    """Non-secret Vertex settings for Claude Code, from the kit .env (project and region only)."""
    from .. import credentials
    project = credentials.get_key("GOOGLE_CLOUD_PROJECT")
    if not project:
        return {}
    location = credentials.get_key("GOOGLE_CLOUD_LOCATION") or "global"
    return {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": project, "CLOUD_ML_REGION": location}


def _build_settings_patch(executors: dict, master_key: str, gateway_url: str,
                          ak_path: str, mode: str, backend: str = "litellm", auth: str = "",
                          helper: str = "") -> dict:
    """Compute the settings.json patch for Claude Code. `auth` is the cockpit's own method (ADR-0015): only
    `vertex_adc` adds env (project and region, never a secret); subscription and api_key write none."""
    env: dict[str, str] = {
        "AGENT_SKILLS_ROOT": f"{ak_path}/skills",
    }
    if auth == "vertex_adc":
        env.update(_vertex_env())
    if mode == "multi-model":
        env["ANTHROPIC_BASE_URL"] = gateway_url

        if backend == "openrouter":
            # OpenRouter authenticates with a bearer token, not x-api-key, and
            # rejects the request outright unless ANTHROPIC_API_KEY is blank —
            # a non-empty value makes Claude Code send both credentials.
            env["ANTHROPIC_AUTH_TOKEN"] = master_key
            env["ANTHROPIC_API_KEY"] = ""
            # Claude Code resolves `/model` aliases to bare Claude IDs, which
            # OpenRouter's namespaced catalogue does not recognise. These pin
            # each alias to a real catalogue entry so the main conversation
            # works. A subagent's frontmatter `model:` overrides them (verified
            # on the wire, 2026-09-16), so they are only the floor.
            for alias, var in (
                ("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
                ("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
                ("haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                ("fable", "ANTHROPIC_DEFAULT_FABLE_MODEL"),
            ):
                pinned = executors.get("classes", {}).get(alias, "")
                if pinned:
                    env[var] = str(pinned)
        # LiteLLM backend: ANTHROPIC_API_KEY is intentionally not set. Claude Code
        # reads its API key from the macOS Keychain ("Claude Code" service) and
        # sends it as x-api-key, ignoring any value in the env block. The gateway
        # master key must equal that Keychain key (handled in the credentials step).

    patch: dict[str, Any] = {
        "env": env,
        "mcpServers": _shared.mcp_engram_block(),
    }
    if mode == "multi-model" and backend == "openrouter":
        # LiteLLM needs no equivalent: it registers every bare Claude ID itself.
        patch["modelOverrides"] = _openrouter_model_overrides()
    if helper:
        patch["apiKeyHelper"] = helper            # a command or `cat <file>`: never the key (ADR-0016 D1)
    return patch


def _resolve_model(configured: str, meta: dict, mode: str, strict: bool = False) -> str:
    """Pick the frontmatter model value for one generated subagent.

    `strict` is set when the host has a recorded model selection: a non-native id that reaches
    single-model there raises `SkipCockpit` instead of silently becoming opus/haiku/inherit. Without
    a selection the legacy fallback stays, so existing setups are unchanged."""
    if mode == "multi-model":
        # Any string is legal: the gateway, not Claude Code, resolves it.
        return configured or "inherit"
    if configured and _is_native_model(configured):
        return configured
    if strict and configured:
        raise _shared.SkipCockpit(
            f"{configured!r} is not a Claude model; Claude Code runs only Claude models without a gateway")
    legacy = str(meta.get("model", "inherit")).lower()
    return {"strong": "opus", "fast": "haiku"}.get(legacy, legacy or "inherit")


def _gate_model(configured: str, mode: str, backend: str, allow_unverified: bool | None) -> str:
    """Per-model unverified gate (FU-1): a non-Anthropic id the compat matrix has not verified for Claude Code
    is not written into a subagent unless `--allow-unverified` was given; the role then inherits the session
    model. `allow_unverified=None` disables the gate (callers that never had it keep their output)."""
    if allow_unverified is None or allow_unverified or mode != "multi-model" or not configured:
        return configured
    from .. import compat
    from ...scoring.db import VENDOR_PROVIDER
    if "/" not in configured:
        return configured                           # a bare Claude id or alias: the verified path
    vendor = configured.split("/", 1)[0]
    provider = VENDOR_PROVIDER.get(vendor, vendor)
    if provider not in compat.PROVIDER_COLUMNS:
        return ""
    cell = compat.cell("claude", provider, compat.MULTI_OPENROUTER if backend == "openrouter" else compat.MULTI_LITELLM)
    return configured if cell.verified and cell.action != compat.SKIP else ""


def _rewrite_body(body: str, ak_path: str) -> str:
    """Rewrite Cursor-isms → Claude Code equivalents."""
    rewrites = (
        ("$AGENT_KIT", ak_path),
        ("**the project's `.cursorrules`**", "project CLAUDE.md"),
        ("`.cursorrules`", "CLAUDE.md"),
        (".cursorrules", "CLAUDE.md"),
        ("`@file`", "Read"),
        ("`@symbol`", "Grep"),
        ("@file", "Read"),
        ("@symbol", "Grep"),
    )
    for old, new in rewrites:
        body = body.replace(old, new)
    return body


def _frontmatter_list(key: str, values) -> list[str]:
    return [f"{key}:", *[f"  - {v}" for v in values]]


def _subagent_text(name: str, desc: str, tools: str | None, model: str, body: str,
                   skills: tuple[str, ...] | list[str] | None = None,
                   mcp_servers: tuple[str, ...] | list[str] | None = None,
                   effort: str | None = None) -> str:
    """The content of one subagent file (pure). `skills`, `mcp_servers` and `effort` are emitted only when
    non-empty, so a role without data renders exactly as before."""
    # Quote defensively: frontmatter is parsed as YAML now, so a description
    # containing a quote or a newline would otherwise emit a broken file.
    safe_desc = " ".join(desc.split()).replace("\\", "\\\\").replace('"', '\\"')
    fm_lines = [f"name: {name}", f'description: "{safe_desc}"']
    if tools is not None:
        fm_lines.append(f"tools: {tools}")
    fm_lines.append(f"model: {model}")
    if effort:
        fm_lines.append(f"effort: {effort}")
    if skills:
        fm_lines += _frontmatter_list("skills", skills)
    if mcp_servers:
        fm_lines += _frontmatter_list("mcpServers", mcp_servers)
    return f"---\n{chr(10).join(fm_lines)}\n---\n\n{body}"


def _write_subagent(name: str, desc: str, tools: str | None, model: str, body: str,
                    skills=None, mcp_servers=None, effort=None) -> bool:
    """Emit one subagent file. Returns True when the file changed on disk."""
    return _shared.write_text(AGENTS_DIR / f"{name}.md",
                              _subagent_text(name, desc, tools, model, body, skills, mcp_servers, effort))


_EFFORT_LINE = re.compile(r"^effort:[ \t]*(\S+)[ \t]*$", re.M)


def _frontmatter_effort(name: str) -> str | None:
    """The `effort:` value currently in the subagent file of `name` (frontmatter only), None when absent."""
    try:
        text = (AGENTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    except OSError:
        return None
    head = text.split("\n---", 2)[0] if text.startswith("---") else ""
    m = _EFFORT_LINE.search(head)
    return m.group(1) if m else None


def effective_effort(name: str, kit_value: str | None, recorded: dict | None) -> tuple[str | None, bool]:
    """(value to render, kept). The kit fills `effort:` only where the operator has not changed it: a file whose value
    differs from what the kit last wrote (`recorded`) keeps that value, and `kept` is True so setup lists it."""
    current = _frontmatter_effort(name)
    if current is not None and current != (recorded or {}).get(name):
        return current, current != kit_value
    return kit_value, False


def _generate_subagent_files(executors: dict, ak_path: str, mode: str,
                             tracking: Any = None, strict: bool = False, sink: Any = None,
                             allow_unverified: bool | None = None, backend: str = "litellm",
                             effort: dict | None = None, recorded_effort: dict | None = None,
                             effort_kept: list | None = None) -> list[str]:
    """Write one Claude Code subagent per kit role and per persona.

    Personas (`agents/personas/<role>-<domain>.md`) are domain overlays with no
    agent of their own: at run time the role subagent reads the overlay itself.
    Composing the two here instead gives each persona a real subagent, and with
    it a `model:` slot — which is the point, since an Angular review and an
    infrastructure review have no reason to cost the same. A persona with no
    `by_persona` entry inherits its role's model, so this changes nothing until
    a profile says otherwise.

    When `tracking` is given, subagents generated by an earlier run that are no
    longer produced are deleted. Only recorded names are removed, so an agent
    the user wrote by hand is never touched.
    """
    specs = role_render.load_specs(repo_root())
    if not specs:
        return []

    by_role = executors.get("by_role", {})
    by_persona = executors.get("by_persona", {})
    generated: list[str] = []
    write = sink or _write_subagent                 # a sink makes this a render-only pass: nothing touches disk
    if sink is None:
        AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    effort_written: dict[str, str] = {}
    for spec in specs:
        body = spec.prompt(lambda b: _rewrite_body(b, ak_path), _shared.kit_context_block(ak_path))
        if spec.is_persona:
            # The persona's model, else its role's: never a silent default.
            configured = str(
                by_persona.get(spec.name, {}).get("model", "")
                or by_role.get(spec.base, {}).get("model", "")
                or ""
            )
            model = _resolve_model(_gate_model(configured, mode, backend, allow_unverified), spec.meta, mode, strict)
        else:
            configured = _gate_model(str(by_role.get(spec.name, {}).get("model", "") or ""), mode, backend,
                                     allow_unverified)
            model = _resolve_model(configured, spec.meta, mode, strict)
        # Append unconditionally: `_write_subagent` reports whether the file changed, which is not the same as
        # whether it should exist. Treating a no-op rewrite as "not produced" made the pruning below delete every
        # agent on any re-run where content was identical.
        kit_effort = None
        if effort and effort_mod.CLAUDE_EFFORT_FRONTMATTER and effort.get(spec.base):
            kit_effort = effort_mod.for_cockpit(effort[spec.base], "claude")
        recorded = recorded_effort if recorded_effort is not None else getattr(tracking, "subagent_effort", None)
        value, kept = effective_effort(spec.name, kit_effort, recorded) if kit_effort else (None, False)
        if kept and effort_kept is not None:
            effort_kept.append(spec.name)
        if value:
            effort_written[spec.name] = kit_effort or value
        extra = {"effort": value} if value else {}
        write(spec.name, spec.description, ROLE_TOOLS.get(spec.base), model, body,
              skill_scope.ROLE_SKILLS.get(spec.base), skill_scope.ROLE_MCP_SERVERS.get(spec.base), **extra)
        generated.append(spec.name)

    if tracking is not None:
        for stale in sorted(set(getattr(tracking, "subagent_files_installed", [])) - set(generated)):
            path = AGENTS_DIR / f"{stale}.md"
            if path.is_file():
                path.unlink()
        tracking.subagent_files_installed = sorted(generated)
        tracking.persona_domains_active = sorted(domains.active_domains())
        if hasattr(tracking, "subagent_effort"):
            tracking.subagent_effort = dict(effort_written)

    return generated


def render_subagent_files(executors: dict, ak_path: str, mode: str, strict: bool = False,
                          allow_unverified: bool | None = None, backend: str = "litellm",
                          effort: dict | None = None, recorded_effort: dict | None = None) -> dict[str, str]:
    """name -> file content for every subagent, without touching disk (the S12 re-render diffs it)."""
    out: dict[str, str] = {}
    _generate_subagent_files(executors, ak_path, mode, None, strict,
                             allow_unverified=allow_unverified, backend=backend,
                             effort=effort, recorded_effort=recorded_effort or {},
                             sink=lambda name, desc, tools, model, body, skills=None, mcp=None, effort=None: out.__setitem__(
                                 name, _subagent_text(name, desc, tools, model, body, skills, mcp, effort)))
    return out


def _shipped_workflow_scripts(ak_path: str) -> list[Path]:
    src_dir = Path(ak_path) / "workflows" / "scripts"
    return sorted(src_dir.glob("kit-*.js")) if src_dir.is_dir() else []


def _install_workflow_scripts(ak_path: str, tracking: state.InstallTracking) -> tuple[list[str], list[str]]:
    """Copy the kit's dynamic workflow scripts to ~/.claude/workflows/ as `/kit-*` commands.

    Copies rather than symlinks: Claude Code rejects a symlinked workflow file. Only scripts a
    previous run of setup installed (recorded in `tracking`) are pruned when the kit stops
    shipping them — a workflow the user wrote themselves is never touched, whatever its name.
    Returns (installed stems, pruned stems).
    """
    scripts = _shipped_workflow_scripts(ak_path)
    if not scripts:
        return [], []
    shipped = {p.name for p in scripts}
    installed: list[str] = []
    for script in scripts:
        if _shared.write_text(WORKFLOWS_DIR / script.name, script.read_text(encoding="utf-8")):
            installed.append(script.stem)

    pruned: list[str] = []
    for name in list(tracking.workflow_scripts_installed):
        if name in shipped:
            continue
        stale = WORKFLOWS_DIR / name
        if stale.is_file():
            stale.unlink()
            pruned.append(stale.stem)
        tracking.workflow_scripts_installed.remove(name)
    for name in sorted(shipped):
        if name not in tracking.workflow_scripts_installed:
            tracking.workflow_scripts_installed.append(name)
    return installed, pruned


# Kit-managed block for ~/.claude/CLAUDE.md. Kept lean on purpose: the whole block is part of every session's prefix.
# It carries the two safety rules that must hold in every session (no long waits inside one tool call; no native
# AskUserQuestion when headless) and the polling guidance, once.
POLLING_GUIDANCE = _shared.WAITING_GUIDANCE


def _claude_md(ak_path: str, gateway_url: str, mode: str) -> str:
    """Kit block for ~/.claude/CLAUDE.md: a short map; details live in skills."""
    return (
        "# ai-resources (Claude Code)\n\n"
        f"Kit root: `{ak_path}`. After `brew upgrade ai-resources`, run `ai-resources setup` to refresh "
        "skills, subagents and hooks.\n\n"
        "## Using the kit\n\n"
        "- **Workflows:** `/kit-plan <goal>` explores, plans from three angles and writes a plan for you to approve; "
        "`/kit-implement` implements it with tests, reviews the diff and verifies each finding. Use them for feature, "
        "bugfix and refactor work. Other flows are the `/workflow-<name>` skills (incident response, k8s debug, "
        "release, ...); load `kit-orchestration` to run one.\n"
        "- **Skills:** only the core ones are listed. Any other kit skill is `" + ak_path + "/skills/<id>/SKILL.md`: read it when the "
        "task matches (a loaded skill overrides generic habits).\n"
        "- **Subagents** for the kit roles are in `~/.claude/agents/` (planner, software-architect, implementer, "
        "tester, code-reviewer, security-auditor, verifier, doc-writer, ...).\n"
        "- **Commands:** `/delegate`, `/setup-project`, `/knowledge-audit`, `/self-update`.\n\n"
        "## Delegation\n\n"
        "- Delegate work that would flood the context (broad searches, long logs, test output), independent review, "
        "and parallel fan-out; do targeted reads and small edits directly.\n"
        "- A delegated prompt carries the workspace path, the goal, the skills to read first, and the return format "
        "from `kit-orchestration`.\n\n"
        f"{POLLING_GUIDANCE}\n"
        f"{_shared.multimodel_protocol_md(ak_path, gateway_url, mode)}"
    )


def is_logged_in_via_oauth() -> bool:
    """Detect OAuth/firstParty mode by querying `claude auth status`.

    Matters in multi-model mode: firstParty auth sends the OAuth session token
    to the LiteLLM gateway instead of the API key, causing 'db not found' errors
    (the gateway has no DB to validate session tokens).

    Primary:  `claude auth status` JSON (apiProvider == "firstParty").
    Fallback: Keychain / credentials-file check (older Claude Code versions).
    """
    claude_bin = shutil.which("claude")
    if claude_bin:
        try:
            r = subprocess.run(
                ["claude", "auth", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                try:
                    data = json.loads(r.stdout.strip())
                    if not data.get("loggedIn", False):
                        return False
                    return (data.get("apiProvider") == "firstParty"
                            or data.get("authMethod") == "claude.ai")
                except (json.JSONDecodeError, AttributeError):
                    pass
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    # Fallback for older versions: keychain / credential-file inspection
    sysname = platform.system()
    if sysname == "Darwin":
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return False
            try:
                creds = json.loads(r.stdout.strip())
                # MCP-only entries have only "mcpOAuth" — not a Claude account session.
                return bool(creds.get("oauthToken") or creds.get("sessionToken")
                            or creds.get("claudeAi") or creds.get("claudeAI"))
            except (json.JSONDecodeError, AttributeError):
                val = r.stdout.strip()
                return bool(val and not val.startswith("sk-ant-") and len(val) > 20)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    if sysname == "Linux":
        for p in (
            Path.home() / ".config" / "Claude" / "credentials.json",
            Path.home() / ".config" / "claude" / "credentials.json",
            Path.home() / ".local" / "share" / "Claude" / "credentials.json",
        ):
            if p.is_file():
                return True
    return False


def _get_keychain_account() -> str:
    """Return the account name stored in the 'Claude Code' Keychain entry."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if '"acct"' in line:
                    m = re.search(r'"acct"<blob>="([^"]+)"', line)
                    if m:
                        return m.group(1)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return Path.home().name


def fix_oauth_for_gateway() -> tuple[bool, str]:
    """Exit OAuth mode and restore the API key for LiteLLM gateway auth.

    In OAuth/firstParty mode Claude Code sends its session token to the gateway
    instead of the API key.  The gateway has no DB to validate session tokens,
    so every request fails with 'db not found'.  This function:

      1. Saves the current API key from Keychain BEFORE logout (logout clears it).
      2. Runs `claude logout` to clear the OAuth session.
      3. Writes the saved key back to Keychain so Claude Code starts in API key
         mode on next launch (no login prompt, no OAuth override).

    Returns (success: bool, message: str).
    """
    from . import _shared as _sh  # noqa: F401 — ensure relative import works
    from .. import credentials as _creds

    if platform.system() != "Darwin":
        return False, "automatic OAuth fix is macOS-only"

    claude_bin = shutil.which("claude")
    if not claude_bin:
        return False, "`claude` binary not found"

    # Step 1 — snapshot key + account before anything changes
    saved_key = _creds.get_claude_code_keychain_key()
    account = _get_keychain_account()

    # Step 2 — logout (clears OAuth session and Keychain entry)
    r = subprocess.run(["claude", "logout"], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return False, f"`claude logout` failed: {r.stderr.strip() or 'non-zero exit'}"

    # Step 3 — restore API key so Claude Code doesn't prompt for login
    if saved_key:
        ok = _creds.write_claude_code_keychain_key(saved_key, account)
        if not ok:
            return (False,
                    "logged out OK but could not restore API key to Keychain — "
                    f"run manually: security add-generic-password -U -s 'Claude Code' "
                    f"-a {account} -w <your-key>")
        return True, "ok-with-key"

    return True, "ok-no-key"


def _install_engram_plugin() -> bool:
    """Install Engram plugin if claude binary is available."""
    if not (shutil.which("engram") and shutil.which("claude")):
        return False
    for cmd in [
        ["claude", "plugin", "marketplace", "add", "Gentleman-Programming/engram"],
        ["claude", "plugin", "install", "engram"],
    ]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return False
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
    return True


def _trim_engram_hooks() -> dict[str, str]:
    """Best-effort: never aborts setup (see engram_patch for the contract)."""
    try:
        from .. import engram_patch
        return engram_patch.trim_engram_hooks()
    except Exception:  # noqa: BLE001
        return {}


def configure(ctx: dict) -> list[Path]:
    """Apply Claude Code configuration. ctx contains: state, executors, master_key.

    Returns list of paths written.
    """
    s = ctx["state"]
    executors = ctx["executors"]
    master_key = ctx.get("master_key", "")
    gateway_url = ctx.get("gateway_url", "http://127.0.0.1:4000")
    ak_path = str(_shared.stable_kit_root(repo_root()))
    mode = s.mode
    backend = getattr(s, "backend", "litellm")
    route = ctx.get("route")
    if route is not None and route.action == "skip" and mode == "multi-model":
        # The matrix has no verified gateway route for the selected models: Claude Code gets the kit
        # content and its own Claude models, and no endpoint or non-Claude model setting (Addendum A2).
        ui.info(f"Claude Code: no gateway setting written ({route.reason})")
        mode = "single-model"
        # The kit's Claude-native roles replace the gateway profile's non-Claude ids: an explicit,
        # announced choice, not a silent fallback inside `_resolve_model`.
        from .. import profiles
        executors = profiles.to_executors(profiles.load_profile("claude-native"))
    strict = getattr(s, "selection", None) is not None

    written: list[Path] = []

    # 1. settings.json (env vars + MCP servers)
    _apply_pending_vertex(s)
    helper_text = cockpit_auth.helper(s, ID)
    helper_setting_text, helper_problem = helper_setting(helper_text) if helper_text else ("", "")
    if helper_problem:
        ui.warn(f"Claude Code: apiKeyHelper not written ({helper_problem}).")
    settings_patch = _build_settings_patch(executors, master_key, gateway_url, ak_path, mode, backend,
                                           auth=cockpit_auth.method(s, ID), helper=helper_setting_text)

    existing: dict = {}
    if SETTINGS_PATH.is_file():
        try:
            existing = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    # Fill-only: a different apiKeyHelper the operator already has is replaced only when they chose one in this run.
    held = existing.get("apiKeyHelper")
    if "apiKeyHelper" in settings_patch and held not in (None, "", settings_patch["apiKeyHelper"]) \
            and not getattr(s, HELPER_OK_ATTR, False):
        del settings_patch["apiKeyHelper"]
        ui.info("Claude Code: your existing apiKeyHelper was kept (change the sign-in to replace it).")

    # Record env keys that teardown must remove.
    # Always include the known multi-model-only keys regardless of whether they were
    # pre-existing (older installs may have written them before tracking existed).
    _apply_cost_env(settings_patch, existing)
    newly_added = _shared.env_keys_added_by_patch(SETTINGS_PATH, settings_patch.get("env", {}))
    if mode == "multi-model":
        s.tracking.cockpit_env_keys_added.setdefault(ID, [])
        for k in _MULTI_MODEL_ONLY_ENV_KEYS + newly_added:
            if k not in s.tracking.cockpit_env_keys_added[ID]:
                s.tracking.cockpit_env_keys_added[ID].append(k)

    # Apply patch
    # A patch that carries the gateway token makes settings.json a key-bearing file: 0600, not the umask default.
    carries_key = bool(settings_patch.get("env", {}).get("ANTHROPIC_AUTH_TOKEN"))
    _shared.deep_merge_json(SETTINGS_PATH, settings_patch, mode=0o600 if carries_key else None)

    if _migrate_agent_teams(SETTINGS_PATH, s.tracking.cost_migrations_applied):
        ui.info(f"Claude Code: removed {AGENT_TEAMS_KEY}=1 (no kit workflow uses agent teams)")

    # Kit hooks: replace earlier kit entries, keep the user's own hooks
    _shared.merge_kit_hooks(SETTINGS_PATH, _kit_hooks(ak_path), _is_kit_hook_command)

    # Cleanup stale hooks
    if SETTINGS_PATH.is_file():
        try:
            cur = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if _cleanup_stale_hooks(cur):
                SETTINGS_PATH.write_text(json.dumps(cur, indent=2, ensure_ascii=False) + "\n",
                                         encoding="utf-8")
        except (json.JSONDecodeError, OSError):
            pass

    # Single-model: proactively remove multi-model-only env keys that may linger
    # from a prior multi-model setup when the tracking record was empty (e.g. the
    # key was pre-existing at setup time so it was never added to cockpit_env_keys_added).
    if mode == "single-model":
        _shared.remove_env_keys_from_settings(SETTINGS_PATH, _MULTI_MODEL_ONLY_ENV_KEYS)
    # Pointing a bare ID at OpenRouter breaks every other route: the Anthropic API
    # and LiteLLM both expect the bare ID itself.
    if not (mode == "multi-model" and backend == "openrouter"):
        _remove_kit_model_overrides(SETTINGS_PATH)

    written.append(SETTINGS_PATH)

    # 2. Subagent files
    sel = s.get_selection() if hasattr(s, "get_selection") else None
    if ctx.get("allow_unverified") and hasattr(s, "profile"):
        s.profile.allow_unverified = True              # persisted: re-renders must honour the same opt-in
    allow_unverified = (bool(ctx.get("allow_unverified")) or bool(getattr(sel, "allow_unverified", False))
                        or bool(getattr(getattr(s, "profile", None), "allow_unverified", False)))
    gated = sorted(r for r, v in (executors.get("by_role") or {}).items()
                   if _gate_model(str((v or {}).get("model", "") or ""), mode, backend, allow_unverified)
                   != str((v or {}).get("model", "") or ""))
    if gated:
        ui.warn("Claude Code: models not verified for this cockpit were not written (roles inherit the session "
                f"model; pass --allow-unverified to write them): {', '.join(gated)}")
    effort_kept: list[str] = []
    agents = _generate_subagent_files(executors, ak_path, mode, s.tracking, strict,
                                      allow_unverified=allow_unverified, backend=backend,
                                      effort=effort_mod.table_for_state(s), effort_kept=effort_kept)
    if effort_kept:
        ui.info("Claude Code: kept your own `effort:` in " + ", ".join(sorted(effort_kept))
                + " (the kit fills effort only where you have not set it).")
    if agents:
        written.append(AGENTS_DIR)

    # 3. CLAUDE.md — only the kit's managed block; the user's content is preserved
    from .. import operator_rules
    md = operator_rules.append_to(_claude_md(ak_path, gateway_url, mode))     # ADR-0016 D4: one rules file, three cockpits
    if _shared.write_managed_block(CLAUDE_MD_PATH, md):
        written.append(CLAUDE_MD_PATH)

    # 4. Dynamic workflow scripts (/kit-plan, /kit-implement)
    scripts, pruned_scripts = _install_workflow_scripts(ak_path, s.tracking)
    written.extend(WORKFLOWS_DIR / f"{name}.js" for name in scripts)
    if pruned_scripts:
        ui.info(f"Removed kit workflow scripts no longer shipped: {', '.join(pruned_scripts)}")

    # 5. Engram plugin (best-effort), then trim its first-message ToolSearch directive (hash-guarded)
    _install_engram_plugin()
    _trim_engram_hooks()

    # 6. Skill links — one per skill, so Claude Code discovers them natively
    # Lean by default: only the skills in skill_scope are listed natively (AI_RESOURCES_SKILLS=all restores every one).
    skills_root = Path(ak_path) / "skills"
    all_ids = [d.name for d in skills_root.iterdir() if (d / "SKILL.md").is_file()] if skills_root.is_dir() else []
    links = _shared.sync_skill_links(CONFIG_ROOT / "skills", skills_root, include=skill_scope.include_list(all_ids))
    if links["removed"]:
        ui.info(f"Removed outdated kit skill links: {', '.join(links['removed'])}")
    if links["skipped"]:
        ui.warn(f"Skills not linked (name already used in {CONFIG_ROOT / 'skills'}): "
                f"{', '.join(links['skipped'])}")

    # Update state with cockpit configuration record
    cs = s.cockpits.get(ID) or state.CockpitState()
    cs.installed = True
    cs.version = ctx.get("detected_version", cs.version)
    cs.binary_path = ctx.get("detected_path", cs.binary_path)
    cs.config_root = str(CONFIG_ROOT)
    cs.configured = True
    cs.last_configured_at = datetime.now(timezone.utc).isoformat()
    s.cockpits[ID] = cs

    return written


def regenerate_agents(executors: dict, mode: str = "multi-model") -> list[str]:
    """Regenerate ~/.claude/agents/*.md from executors.yaml without running full setup.

    Returns list of role names written. Safe to call standalone after updating executors.
    """
    ak_path = str(_shared.stable_kit_root(repo_root()))
    try:
        s = state.load()
    except Exception:  # noqa: BLE001 — an unreadable state file must not block regeneration
        return _generate_subagent_files(executors, ak_path, mode)
    names = _generate_subagent_files(executors, ak_path, mode, s.tracking)
    owned = list(s.tracking.subagent_files_installed)

    def record(fresh: state.SetupState) -> None:
        # only the record of the subagent files is this function's: a setup run that saved in the meantime
        # keeps every other field it wrote
        fresh.tracking.subagent_files_installed = owned

    if not state.update(record):
        ui.warn("The setup state is busy (another ai-resources run holds its lock): the agent files were "
                "regenerated but their record was not updated. Run `ai-resources generate` again later.")
    return names


def teardown(env_keys: list[str]) -> list[str]:
    """Undo what setup put in place for this cockpit. Returns the env keys actually removed.

    Removes the kit's workflow scripts too: they call kit role subagents, so leaving them behind
    after the agents are gone would give the user `/kit-*` commands that fail mid-run. Only scripts
    a previous setup recorded as installed are deleted, so a file the user happens to name
    `kit-plan.js` survives. Note that `brew uninstall` runs nothing — this path is the mode switch,
    so a full uninstall still leaves the generated files in place.
    """
    try:
        tracked = set(state.load().tracking.workflow_scripts_installed)
    except Exception:  # noqa: BLE001 — a missing or unreadable state file must not block teardown
        tracked = set()
    for script in _shipped_workflow_scripts(str(_shared.stable_kit_root(repo_root()))):
        installed = WORKFLOWS_DIR / script.name
        if script.name in tracked and installed.is_file():
            installed.unlink()
    _remove_kit_model_overrides(SETTINGS_PATH)
    return _shared.remove_env_keys_from_settings(SETTINGS_PATH, env_keys)


# --- verify (read-only) ----------------------------------------------------------------------------------------

def _recorded_names(value: Any) -> list[str]:
    """A tracking list of names, or [] when the record is not the shape this expects."""
    return [v for v in value if isinstance(v, str) and v] if isinstance(value, list) else []


def verify(ctx: dict) -> list:
    """What setup left in Claude Code: the managed CLAUDE.md block, settings.json, and the
    workflow scripts, subagents and skill links the state file recorded. Writes nothing, and
    judges nothing about the model or permission settings."""
    from ...verify import Finding as F, block_findings

    s = ctx["state"]
    cs = s.cockpits.get(ID)
    if not getattr(cs, "configured", False):
        return [F("ok", ID, "claude is not configured by the kit; nothing to verify")]
    out = block_findings(ID, CLAUDE_MD_PATH, "the kit block")
    if not SETTINGS_PATH.is_file():
        out.append(F("warn", ID, f"{SETTINGS_PATH} is missing although setup configured claude", "re-run `ai-resources setup`"))
    else:
        try:
            ok = isinstance(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")), dict)
        except (OSError, ValueError):
            ok = False
        out.append(F("ok", ID, f"{SETTINGS_PATH} is valid JSON") if ok else
                   F("error", ID, f"{SETTINGS_PATH} is not valid JSON, so Claude Code ignores it",
                     "fix or remove the file, then re-run `ai-resources setup`"))
    tracking = s.tracking
    gone = [n for n in _recorded_names(getattr(tracking, "workflow_scripts_installed", None))
            if not (WORKFLOWS_DIR / n).is_file()]
    gone += [f"agents/{n}.md" for n in _recorded_names(getattr(tracking, "subagent_files_installed", None))
             if not (AGENTS_DIR / f"{n}.md").is_file()]
    if gone:
        out.append(F("warn", ID, f"{len(gone)} file(s) setup installed are missing: {', '.join(gone[:6])}"
                     + (" ..." if len(gone) > 6 else ""), "re-run `ai-resources setup`"))
    skills = CONFIG_ROOT / "skills"
    if not skills.is_dir():
        out.append(F("warn", ID, f"{skills} does not exist, so no kit skill is linked", "re-run `ai-resources setup`"))
    return out
