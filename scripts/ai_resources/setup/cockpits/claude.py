"""Claude Code cockpit configurator — full multi-model support."""
from __future__ import annotations

import json
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
from ...generate import parse_simple_frontmatter
from . import _shared


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
_MULTI_MODEL_ONLY_ENV_KEYS: list[str] = [
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
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
KIT_HOOK_SCRIPTS = ("kit_session_start.py", "kit_subagent_return.py", "kit_handoff_guard.py")


def _is_native_model(model: str) -> bool:
    """True for values Claude Code resolves without a gateway."""
    return model in NATIVE_MODELS or model.startswith("claude-")


def _is_kit_hook_command(command: str) -> bool:
    return any(f"/hooks/{script}" in command for script in KIT_HOOK_SCRIPTS)


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
        "PreToolUse": [entry("kit_handoff_guard.py", "Write|Edit|MultiEdit|NotebookEdit|Bash")],
    }


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


def _build_settings_patch(executors: dict, master_key: str, gateway_url: str,
                          ak_path: str, mode: str, backend: str = "litellm") -> dict:
    """Compute the settings.json patch for Claude Code."""
    env: dict[str, str] = {
        "AGENT_SKILLS_ROOT": f"{ak_path}/skills",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
    }
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

    return {
        "env": env,
        "mcpServers": _shared.mcp_engram_block(),
    }


def _resolve_model(configured: str, meta: dict, mode: str) -> str:
    """Pick the frontmatter model value for one generated subagent."""
    if mode == "multi-model":
        # Any string is legal: the gateway, not Claude Code, resolves it.
        return configured or "inherit"
    if configured and _is_native_model(configured):
        return configured
    legacy = str(meta.get("model", "inherit")).lower()
    return {"strong": "opus", "fast": "haiku"}.get(legacy, legacy or "inherit")


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


def _write_subagent(name: str, desc: str, tools: str | None, model: str, body: str) -> bool:
    """Emit one subagent file. Returns True when the file changed on disk."""
    # Quote defensively: frontmatter is parsed as YAML now, so a description
    # containing a quote or a newline would otherwise emit a broken file.
    safe_desc = " ".join(desc.split()).replace("\\", "\\\\").replace('"', '\\"')
    fm_lines = [f"name: {name}", f'description: "{safe_desc}"']
    if tools is not None:
        fm_lines.append(f"tools: {tools}")
    fm_lines.append(f"model: {model}")
    content = f"---\n{chr(10).join(fm_lines)}\n---\n\n{body}"
    return _shared.write_text(AGENTS_DIR / f"{name}.md", content)


def _generate_subagent_files(executors: dict, ak_path: str, mode: str,
                             tracking: Any = None) -> list[str]:
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
    roles_dir = repo_root() / "agents" / "roles"
    if not roles_dir.is_dir():
        return []

    by_role = executors.get("by_role", {})
    by_persona = executors.get("by_persona", {})
    generated: list[str] = []
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    role_bodies: dict[str, str] = {}

    for role_md in sorted(roles_dir.glob("*.md")):
        try:
            text = role_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, body = parse_simple_frontmatter(text)
        name = str(meta.get("name", role_md.stem))
        body = _rewrite_body(body, ak_path)
        role_bodies[name] = body

        model = _resolve_model(str(by_role.get(name, {}).get("model", "") or ""), meta, mode)
        if _write_subagent(name, str(meta.get("description", "")),
                           ROLE_TOOLS.get(name), model,
                           body + _shared.kit_context_block(ak_path)):
            generated.append(name)

    personas_dir = repo_root() / "agents" / "personas"
    for persona_md in sorted(personas_dir.glob("*.md")) if personas_dir.is_dir() else []:
        name = persona_md.stem
        # Longest match wins: both halves of `<role>-<domain>` can contain
        # hyphens, so `code-reviewer-api-platform` cannot be split on one.
        candidates = [r for r in role_bodies if name.startswith(f"{r}-")]
        if not candidates:
            continue
        base = max(candidates, key=len)
        try:
            text = persona_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, overlay = parse_simple_frontmatter(text)

        # The persona's model, else its role's: never a silent default.
        configured = str(
            by_persona.get(name, {}).get("model", "")
            or by_role.get(base, {}).get("model", "")
            or ""
        )
        model = _resolve_model(configured, meta, mode)
        body = (role_bodies[base] + "\n\n---\n\n" + _rewrite_body(overlay, ak_path)
                + _shared.kit_context_block(ak_path))
        if _write_subagent(name, str(meta.get("description", "")),
                           ROLE_TOOLS.get(base), model, body):
            generated.append(name)

    if tracking is not None:
        for stale in sorted(set(getattr(tracking, "subagent_files_installed", [])) - set(generated)):
            path = AGENTS_DIR / f"{stale}.md"
            if path.is_file():
                path.unlink()
        tracking.subagent_files_installed = sorted(generated)

    return generated


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


def _claude_md(ak_path: str, gateway_url: str, mode: str) -> str:
    """Kit block for ~/.claude/CLAUDE.md — a short map; details live in skills."""
    return (
        "# ai-resources (Claude Code)\n\n"
        f"Kit root: `{ak_path}`. After `brew upgrade ai-resources`, run `ai-resources setup` to refresh "
        "skills, subagents and hooks.\n\n"
        "## Using the kit\n\n"
        "- **Skills** are installed as native skills. Load one when its description matches the task; "
        "a loaded skill overrides generic habits.\n"
        "- **Workflows** are the `workflow-*` skills (feature, bugfix, refactor, incident response, …). "
        "Use one for multi-step work that matches its description; the `kit-orchestration` skill "
        "explains how to run steps, handoffs and parallel groups.\n"
        "- **Deterministic core loop:** `/kit-plan <goal>` explores, drafts plans from three angles and "
        "writes one for you to approve; `/kit-implement` then implements it with tests, reviews the diff "
        "from three lenses, verifies each finding and signs off. Use them for feature, bugfix and refactor "
        "work instead of running those steps by hand.\n"
        "- **Subagents** for the kit roles are in `~/.claude/agents/` (planner, software-architect, "
        "implementer, tester, code-reviewer, security-auditor, verifier, doc-writer, …).\n"
        "- **Commands:** `/delegate`, `/setup-project`, `/knowledge-audit`, `/self-update`.\n\n"
        "## Delegation\n\n"
        "Decide by task shape, not by step or file count:\n\n"
        "- Delegate work that would flood the context (broad searches, long logs, test output), "
        "independent review, and parallelizable fan-out.\n"
        "- Do targeted reads and small edits directly.\n"
        "- A delegated prompt carries the workspace path, the goal, the skills to read first, and the "
        "return format from `kit-orchestration`.\n\n"
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

    written: list[Path] = []

    # 1. settings.json (env vars + MCP servers)
    settings_patch = _build_settings_patch(executors, master_key, gateway_url, ak_path, mode, backend)

    existing: dict = {}
    if SETTINGS_PATH.is_file():
        try:
            existing = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    # Record env keys that teardown must remove.
    # Always include the known multi-model-only keys regardless of whether they were
    # pre-existing (older installs may have written them before tracking existed).
    newly_added = _shared.env_keys_added_by_patch(SETTINGS_PATH, settings_patch.get("env", {}))
    if mode == "multi-model":
        s.tracking.cockpit_env_keys_added.setdefault(ID, [])
        for k in _MULTI_MODEL_ONLY_ENV_KEYS + newly_added:
            if k not in s.tracking.cockpit_env_keys_added[ID]:
                s.tracking.cockpit_env_keys_added[ID].append(k)

    # Apply patch
    _shared.deep_merge_json(SETTINGS_PATH, settings_patch)

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

    written.append(SETTINGS_PATH)

    # 2. Subagent files
    agents = _generate_subagent_files(executors, ak_path, mode, s.tracking)
    if agents:
        written.append(AGENTS_DIR)

    # 3. CLAUDE.md — only the kit's managed block; the user's content is preserved
    md = _claude_md(ak_path, gateway_url, mode)
    if _shared.write_managed_block(CLAUDE_MD_PATH, md):
        written.append(CLAUDE_MD_PATH)

    # 4. Dynamic workflow scripts (/kit-plan, /kit-implement)
    scripts, pruned_scripts = _install_workflow_scripts(ak_path, s.tracking)
    written.extend(WORKFLOWS_DIR / f"{name}.js" for name in scripts)
    if pruned_scripts:
        ui.info(f"Removed kit workflow scripts no longer shipped: {', '.join(pruned_scripts)}")

    # 5. Engram plugin (best-effort)
    _install_engram_plugin()

    # 6. Skill links — one per skill, so Claude Code discovers them natively
    links = _shared.sync_skill_links(CONFIG_ROOT / "skills", Path(ak_path) / "skills")
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
    state.save(s)
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
    return _shared.remove_env_keys_from_settings(SETTINGS_PATH, env_keys)
