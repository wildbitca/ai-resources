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


# Tools each role may use — used when generating subagent files.
ROLE_TOOLS: dict[str, str | None] = {
    "explore":            "Read, Grep, Glob",
    "planner":            "Read, Grep, Glob, Bash",
    "implementer":        "Read, Edit, Write, Bash, Grep, Glob",
    "tester":             "Read, Edit, Write, Bash, Grep, Glob",
    "code-reviewer":      "Read, Bash, Grep, Glob",
    "security-auditor":   "Read, Bash, Grep, Glob",
    "software-architect": "Read, Grep, Glob, Bash",
    "verifier":           "Read, Bash, Grep, Glob",
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


def _cleanup_stale_hooks(settings: dict) -> bool:
    """Remove deprecated UserPromptSubmit hook (legacy from earlier kit versions)."""
    hooks = settings.get("hooks")
    if isinstance(hooks, dict) and "UserPromptSubmit" in hooks:
        del hooks["UserPromptSubmit"]
        return True
    return False


def _build_settings_patch(executors: dict, master_key: str, gateway_url: str,
                          ak_path: str, mode: str) -> dict:
    """Compute the settings.json patch for Claude Code."""
    env: dict[str, str] = {
        "AGENT_SKILLS_ROOT": f"{ak_path}/skills",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
    }
    if mode == "multi-model":
        env["ANTHROPIC_BASE_URL"] = gateway_url
        # ANTHROPIC_API_KEY is intentionally not set here: Claude Code reads its
        # API key from the macOS Keychain ("Claude Code" service) and sends it as
        # x-api-key, ignoring any value in the env block. The gateway master key
        # must equal that Keychain key (handled in the credentials wizard step).

    return {
        "env": env,
        "mcpServers": _shared.mcp_engram_block(),
    }


def _generate_subagent_files(executors: dict, ak_path: str, mode: str) -> list[str]:
    """Walk agents/roles/*.md from kit, write a Claude Code subagent file per role
    with its assigned model from executors.yaml.
    """
    roles_dir = repo_root() / "agents" / "roles"
    if not roles_dir.is_dir():
        return []

    by_role = executors.get("by_role", {})
    generated: list[str] = []
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    for role_md in sorted(roles_dir.glob("*.md")):
        try:
            text = role_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, body = parse_simple_frontmatter(text)
        name = str(meta.get("name", role_md.stem))
        desc = str(meta.get("description", ""))
        tools = ROLE_TOOLS.get(name)

        # Resolve model: from executors mapping (multi-model) or fallback to inherit (single)
        if mode == "multi-model":
            cfg = by_role.get(name, {})
            model = cfg.get("model", "inherit")
        else:
            # single-model: keep legacy inherit/opus/haiku behavior
            legacy = str(meta.get("model", "inherit")).lower()
            model = {"strong": "opus", "fast": "haiku"}.get(legacy, legacy or "inherit")

        fm_lines = [f"name: {name}", f'description: "{desc}"']
        if tools is not None:
            fm_lines.append(f"tools: {tools}")
        fm_lines.append(f"model: {model}")

        # Rewrite Cursor-isms → Claude Code equivalents
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
        body += _shared.kit_context_block(ak_path)

        out_path = AGENTS_DIR / f"{name}.md"
        content = f"---\n{chr(10).join(fm_lines)}\n---\n\n{body}"
        if _shared.write_text(out_path, content):
            generated.append(name)

    return generated


def _scan_workflow_triggers(ak_path: str) -> list[tuple[str, str, str]]:
    wf_dir = Path(ak_path) / "workflows"
    if not wf_dir.is_dir():
        return []
    rows: list[tuple[str, str, str]] = []
    for wf in sorted(wf_dir.glob("*.workflow.yaml")):
        text = wf.read_text(encoding="utf-8", errors="replace")
        name_m = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
        trig_m = re.search(r"^trigger:\s*(.+)$", text, re.MULTILINE)
        if name_m and trig_m:
            rows.append((wf.name, name_m.group(1).strip(), trig_m.group(1).strip()))
    return rows


def _claude_md(ak_path: str, gateway_url: str, mode: str) -> str:
    """Build CLAUDE.md content."""
    refresh = (
        f"After updating the kit (e.g. `brew upgrade ai-resources` or `git pull`), "
        f"run `ai-resources generate`."
    )

    rows = _scan_workflow_triggers(ak_path)
    workflow_table = ""
    if rows:
        workflow_table = "| Workflow | Trigger |\n|----------|--------|\n"
        for filename, _name, trigger in rows:
            workflow_table += f"| `{filename}` | {trigger} |\n"
        workflow_table = (
            f"## Workflow Discovery Protocol (MANDATORY — checked FIRST)\n\n"
            f"**Before starting ANY non-trivial task**, check if a workflow applies:\n\n"
            f"1. **Match** — Compare the user's task against the workflow triggers below:\n\n"
            f"{workflow_table}\n"
            f"2. **Load** — Read its full YAML at `{ak_path}/workflows/` and follow phase by phase.\n"
            f"3. **MANDATORY** — Follow the workflow's defined phases. Do NOT substitute built-in tools.\n"
            f"4. **Handoff** — Use `.agent-output/handoff-<branch>.md` per `WORKFLOW_CONTRACT.md`.\n"
            f"5. **No match?** — For trivial tasks, proceed without a workflow.\n\n"
        )

    skill_recipe = (
        f"## Skill Discovery Protocol\n\n"
        f"**On every task**, follow this protocol:\n\n"
        f"1. **Read the catalog** — `{ak_path}/skills-index.json`\n"
        f"2. **Match** — Compare your current task against each skill's description and triggers.\n"
        f"3. **Load** — For each matching skill, read its full SKILL.md.\n"
        f"4. **Apply** — Skill authority overrides generic patterns.\n"
        f"5. **No match?** — Proceed normally.\n\n"
        f"### Key paths\n\n"
        f"| Resource | Path |\n"
        f"|----------|------|\n"
        f"| Skills index | `{ak_path}/skills-index.json` |\n"
        f"| Skills root | `{ak_path}/skills/` |\n"
        f"| Workflows | `{ak_path}/workflows/` |\n"
        f"| Workflow contract | `{ak_path}/workflows/WORKFLOW_CONTRACT.md` |\n"
        f"| Kit docs | `{ak_path}/AGENTS.md` |\n\n"
    )

    multimodel = _shared.multimodel_protocol_md(ak_path, gateway_url, mode)

    agents_section = _build_agent_teams_section()

    return (
        f"# ai-resources (Claude Code)\n\n"
        f"**Refresh:** {refresh}\n\n"
        f"{multimodel}"
        f"{workflow_table}"
        f"{skill_recipe}"
        f"{agents_section}"
    )


def _build_agent_teams_section() -> str:
    """Build the Subagent Definitions + Agent Teams section."""
    if not AGENTS_DIR.is_dir():
        return ""
    names = sorted(p.stem for p in AGENTS_DIR.glob("*.md"))
    if not names:
        return ""

    rows = "| Agent | Model | Use for |\n|-------|-------|--------|\n"
    for n in names:
        try:
            m, _ = parse_simple_frontmatter((AGENTS_DIR / f"{n}.md").read_text(encoding="utf-8", errors="replace"))
            d = str(m.get("description", "")).strip('"')[:80]
            mdl = str(m.get("model", "inherit"))
        except OSError:
            d = ""
            mdl = "inherit"
        rows += f"| `{n}` | `{mdl}` | {d} |\n"

    return (
        f"## Subagent Definitions (auto-generated)\n\n{rows}\n"
        f"Each subagent's `model:` is mapped via `~/.config/ai-resources/executors.yaml`.\n"
        f"Edit there and re-run `ai-resources setup` to change the assignment.\n\n"
        f"### Agent Teams\n\n"
        f"Spawn teams via `TeamCreate` for parallel multi-step workflows. "
        f"See `{repo_root()}/workflows/` for blueprints.\n"
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
    ak_path = str(repo_root())
    mode = s.mode

    written: list[Path] = []

    # 1. settings.json (env vars + MCP servers)
    settings_patch = _build_settings_patch(executors, master_key, gateway_url, ak_path, mode)

    existing: dict = {}
    if SETTINGS_PATH.is_file():
        try:
            existing = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    # Record which env keys are net-new (so we can later remove only those on teardown)
    newly_added = _shared.env_keys_added_by_patch(SETTINGS_PATH, settings_patch.get("env", {}))
    if mode == "multi-model" and newly_added:
        s.tracking.cockpit_env_keys_added.setdefault(ID, [])
        for k in newly_added:
            if k not in s.tracking.cockpit_env_keys_added[ID]:
                s.tracking.cockpit_env_keys_added[ID].append(k)

    # Apply patch
    _shared.deep_merge_json(SETTINGS_PATH, settings_patch)

    # Cleanup stale hooks
    if SETTINGS_PATH.is_file():
        try:
            cur = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if _cleanup_stale_hooks(cur):
                SETTINGS_PATH.write_text(json.dumps(cur, indent=2, ensure_ascii=False) + "\n",
                                         encoding="utf-8")
        except (json.JSONDecodeError, OSError):
            pass
    written.append(SETTINGS_PATH)

    # 2. Subagent files
    agents = _generate_subagent_files(executors, ak_path, mode)
    if agents:
        written.append(AGENTS_DIR)

    # 3. CLAUDE.md
    md = _claude_md(ak_path, gateway_url, mode)
    if _shared.write_text(CLAUDE_MD_PATH, md):
        written.append(CLAUDE_MD_PATH)

    # 4. Engram plugin (best-effort)
    _install_engram_plugin()

    # 5. Skill symlink
    link = CONFIG_ROOT / "skills" / "ai-resources"
    target = repo_root() / "skills"
    _shared.ensure_symlink(link, target)

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
    ak_path = str(repo_root())
    return _generate_subagent_files(executors, ak_path, mode)


def teardown(env_keys: list[str]) -> list[str]:
    """Remove the listed env keys from settings.json. Returns keys actually removed."""
    return _shared.remove_env_keys_from_settings(SETTINGS_PATH, env_keys)
