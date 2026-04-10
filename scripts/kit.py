#!/usr/bin/env python3
"""
ai-resources kit CLI — generate (skills + index), setup (IDE + validation), version.

  python3 scripts/kit.py --help
  python3 scripts/kit.py COMMAND --help
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.7.1"

# Repo root = parent of scripts/
REPO = Path(__file__).resolve().parents[1]
META_NAME = ".skill-source.yaml"


def _user_state_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "ai-resources" / "state.json"


def _write_setup_state(agent_kit: Path, targets: list[str]) -> None:
    """Record last setup for humans / tools (optional; does not override AGENT_KIT resolution)."""
    path = _user_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "agent_kit": str(agent_kit.resolve()),
        "targets": targets,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Recorded setup in {path}")


# --- resources.json -----------------------------------------------------------------
def resolve_resources_path() -> Path:
    return REPO / "resources.json"


def read_resources() -> dict:
    path = resolve_resources_path()
    if not path.is_file():
        raise FileNotFoundError(f"Missing resources manifest: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# --- skill id flattening (workflows) ----------------------------------------------
def workflow_skill_id_to_flat(sid: str) -> str:
    sid = sid.strip()
    if "/" not in sid:
        return sid
    parts = sid.split("/")
    if parts[0] == "vendor" and len(parts) >= 2 and parts[1] == "gentleman-programming":
        rest = "-".join(parts[2:])
        rest = rest.replace("gentleman-programming-", "")
        return "gpm-" + rest
    return parts[0] + "-" + "-".join(parts[1:])


# --- generate (skills-index.json) -------------------------------------------------
def _default_agent_kit() -> Path:
    env = os.environ.get("AGENT_KIT")
    if env:
        return Path(env)
    return REPO


def _get_frontmatter_raw(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    return text[3:end].strip()


def _parse_simple_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end].strip()
    meta: dict[str, object] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            continue
        m = re.match(r"^([a-zA-Z0-9_-]+):\s*(.*)$", line)
        if m:
            k, v = m.group(1), m.group(2).strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            meta[k] = v
    body = text[end + 4:].lstrip()
    return meta, body


def _parse_list_field(block: str, field: str) -> list[str]:
    pattern = rf"^{field}:\s*\n((?:\s*-\s+.+\n?)+)"
    m = re.search(pattern, block, re.MULTILINE)
    if not m:
        return []
    out: list[str] = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if line.startswith("- "):
            out.append(line[2:].strip()[:500])
    return out


def _extract_triggers_from_description(meta: dict[str, object], body: str, block: str = "") -> list[str]:
    out: list[str] = []
    desc = str(meta.get("description", "") or "")
    if "Trigger:" in desc:
        part = desc.split("Trigger:", 1)[1].strip()
        out.append(part[:500])
    if "(triggers:" in desc:
        m = re.search(r"\(triggers?:\s*([^)]+)\)", desc, re.I)
        if m:
            out.append(m.group(1).strip()[:500])
    # Check raw frontmatter block (handles multi-line YAML scalars like description: >)
    if not out and block:
        m = re.search(r"\(triggers?:\s*([^)]+)\)", block, re.I)
        if m:
            out.append(m.group(1).strip()[:500])
    m = re.search(r"\(triggers?:\s*([^)]+)\)", body[:5000], re.I)
    if m:
        out.append(m.group(1).strip()[:500])
    return out


def _cmd_generate_index() -> int:
    """Emit skills-index.json from AGENT_SKILLS_ROOT (no vendor import)."""
    ak = _default_agent_kit()
    root = Path(os.environ.get("AGENT_SKILLS_ROOT", ak / "skills"))
    if not root.is_dir():
        print(f"Skills root not found: {root}", file=sys.stderr)
        return 1

    out_path = Path(os.environ.get("SKILLS_INDEX_OUT", ak / "skills-index.json"))

    entries: list[dict] = []
    for skill_md in sorted(root.rglob("SKILL.md")):
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        block = _get_frontmatter_raw(text)
        meta, body = _parse_simple_frontmatter(text)

        rel_parent = skill_md.parent.relative_to(root).as_posix()
        sid = rel_parent
        name = str(meta.get("name", rel_parent.replace("/", "-")))
        desc = str(meta.get("description", ""))[:2000]

        triggers_list = _parse_list_field(block, "triggers") if block else []
        globs_list = _parse_list_field(block, "globs") if block else []
        # Support inline triggers field (triggers: "val1, val2") in addition to YAML list
        if not triggers_list and meta.get("triggers"):
            triggers_list = [str(meta["triggers"])[:500]]
        triggers = triggers_list if triggers_list else _extract_triggers_from_description(meta, body, block or "")
        # Support inline globs field (globs: "val1", "val2") in addition to YAML list
        if not globs_list and meta.get("globs"):
            globs_list = [str(meta["globs"])[:500]]
        globs = globs_list

        entries.append(
            {
                "id": sid,
                "path": str(skill_md),
                "name": name,
                "description": desc,
                "triggers": triggers,
                "globs": globs,
            }
        )

    doc = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "skills_root": str(root.resolve()),
        "count": len(entries),
        "skills": entries,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} ({len(entries)} skills)")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    """Vendor-sync (unless --skip-vendor) then rebuild skills-index.json."""
    if not getattr(args, "skip_vendor", False):
        rc = _cmd_import_skills(args)
        if rc != 0:
            return rc
        print("==> skills-index.json", file=sys.stderr)
    return _cmd_generate_index()


# --- generate: import skills from resources.json (internal) ------------------------
def _normalize_config(data: dict) -> dict:
    if "skills" not in data:
        raise ValueError("resources.json must contain a top-level 'skills' object")
    out = dict(data)
    out.setdefault("agents", {"sources": []})
    out.setdefault("personas", {"sources": []})
    out["skills"].setdefault("sources", [])
    return out


def _run_git_clone(url: str, ref: str, depth: int, dest: Path) -> None:
    subprocess.run(
        ["git", "clone", "--depth", str(depth), "--branch", ref, url, str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_head(clone: Path) -> str:
    r = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    return (r.stdout or "").strip() or "unknown"


def _skill_id_for(prefix: str, rel: Path) -> str:
    s = rel.as_posix().replace("/", "-")
    return f"{prefix}-{s}"


def _write_meta(dest: Path, source_id: str, from_rel: str, revision: str, _url: str) -> None:
    body = (
        f"# Auto-generated by kit.py (generate import)\n"
        f"source_id: {source_id}\n"
        f"from_path: {from_rel}\n"
        f"git_revision: {revision}\n"
    )
    (dest / META_NAME).write_text(body, encoding="utf-8")


def _sync_source(
        src: dict,
        skills_root: Path,
        dry_run: bool,
        force: bool,
) -> list[str]:
    errors: list[str] = []
    sid = src["id"]
    url = src["url"]
    ref = src.get("ref", "main")
    depth = int(src.get("depth", 1))
    prefix = src.get("skill_id_prefix", sid.split("-")[0])
    roots = src.get("import_roots", ["curated", "community"])
    managed_prefix = f"{prefix}-"

    with tempfile.TemporaryDirectory(prefix=f"skill-src-{sid}-") as tmp:
        clone = Path(tmp) / "src"
        _run_git_clone(url, ref, depth, clone)
        rev = _git_head(clone)

        for root_name in roots:
            base = clone / root_name
            if not base.is_dir():
                continue
            for skill_md in sorted(base.rglob("SKILL.md")):
                rel = skill_md.parent.relative_to(clone)
                sk_id = _skill_id_for(prefix, rel)
                dest = skills_root / sk_id
                from_rel = rel.as_posix()
                meta_path = dest / META_NAME

                if dest.exists():
                    if meta_path.is_file():
                        existing = meta_path.read_text(encoding="utf-8", errors="replace")
                        if f"source_id: {sid}" not in existing and not force:
                            errors.append(
                                f"CONFLICT: {dest.name} exists, managed by different source (expected {sid})"
                            )
                            continue
                    else:
                        if not sk_id.startswith(managed_prefix):
                            errors.append(
                                f"CONFLICT: {dest.name} blocks import (not {managed_prefix}*, no {META_NAME})"
                            )
                            continue
                        if not force:
                            errors.append(
                                f"CONFLICT: {dest.name} exists without {META_NAME}; use --force to replace from {sid}"
                            )
                            continue

                if dry_run:
                    print(f"would sync {from_rel} -> skills/{sk_id}/ @ {rev[:8]}")
                    continue

                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(skill_md.parent, dest)
                _write_meta(dest, sid, from_rel, rev, url)
                print(f"synced {sk_id} <= {from_rel} ({rev[:8]})")

    return errors


def _cmd_import_skills(args: argparse.Namespace) -> int:
    cfg_path = resolve_resources_path()
    if not cfg_path.is_file():
        print(f"Missing {cfg_path}", file=sys.stderr)
        return 1

    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg = _normalize_config(data)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    skills_root = Path(os.environ.get("AGENT_SKILLS_ROOT", REPO / "skills"))
    os.environ.setdefault("AGENT_SKILLS_ROOT", str(skills_root))

    agents_n = len(cfg.get("agents", {}).get("sources", []))
    personas_n = len(cfg.get("personas", {}).get("sources", []))
    if agents_n or personas_n:
        print(
            "Note: resources.json lists agents/personas sources — sync not implemented yet; "
            "only skills.*.sources are processed.",
            file=sys.stderr,
        )

    all_err: list[str] = []
    for src in cfg.get("skills", {}).get("sources", []):
        if src.get("type") != "git":
            print(f"Skip unsupported type: {src.get('type')}", file=sys.stderr)
            continue
        all_err.extend(_sync_source(src, skills_root, args.dry_run, args.force))

    if all_err:
        print("\ngenerate (skills import): FAILED", file=sys.stderr)
        for e in all_err:
            print(f"  {e}", file=sys.stderr)
        return 2
    print("generate (skills import): OK")
    return 0


# --- setup: MCP presets (internal) --------------------------------------------------
def _expand_path(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p)))


def _apply_mcp_config(args: argparse.Namespace) -> int:
    try:
        data = read_resources()
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1

    if args.target in ("cursor", "all"):
        mcp = data.get("mcp") or {}
        cur = mcp.get("cursor")
        if cur is None:
            return 0
        if "mcpServers" not in cur:
            print("mcp.cursor present but missing mcpServers — skipping", file=sys.stderr)
            return 0
        body = {"mcpServers": cur["mcpServers"]}
        out = cur.get("path") or "~/.cursor/mcp.json"
        dest = _expand_path(out)
        if args.dry_run:
            n = len(body.get("mcpServers") or {})
            print(f"would write {dest} ({n} mcpServers entries)")
            return 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote {dest}")
    return 0


# --- setup (IDE pointers + MCP + workflow validation) -------------------------------
def _kit_refresh_hint(ak: Path) -> str:
    r = str(ak)
    return (
        "After updating the kit (e.g. `brew upgrade <formula>` or `git pull` in a source checkout), run "
        f"`python3 {r}/scripts/kit.py generate`."
    )


def _scan_workflow_triggers(ak_s: str) -> list[tuple[str, str, str]]:
    """Scan workflow YAML files and extract (filename, name, trigger) tuples."""
    wf_dir = Path(ak_s) / "workflows"
    if not wf_dir.is_dir():
        return []
    rows: list[tuple[str, str, str]] = []
    for wf in sorted(wf_dir.glob("*.workflow.yaml")):
        text = wf.read_text(encoding="utf-8", errors="replace")
        name_m = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
        trigger_m = re.search(r"^trigger:\s*(.+)$", text, re.MULTILINE)
        if name_m and trigger_m:
            rows.append((wf.name, name_m.group(1).strip(), trigger_m.group(1).strip()))
    return rows


def _workflow_recipe(ak_s: str) -> str:
    """Return the workflow discovery protocol section for agent stubs."""
    rows = _scan_workflow_triggers(ak_s)
    if not rows:
        return ""

    table_lines = "| Workflow | Trigger |\n|----------|--------|\n"
    for filename, _name, trigger in rows:
        table_lines += f"| `{filename}` | {trigger} |\n"

    return (
        f"## Workflow Discovery Protocol (MANDATORY — checked FIRST)\n\n"
        f"**Before starting ANY non-trivial task**, check if a workflow applies:\n\n"
        f"1. **Match** — Compare the user's task against the workflow triggers below:\n\n"
        f"{table_lines}\n"
        f"2. **Load** — If a workflow matches, read its full YAML at `{ak_s}/workflows/` "
        f"and follow it phase by phase.\n"
        f"3. **MANDATORY**: When a workflow matches, you MUST follow its defined phases "
        f"(e.g. research → plan → architect → implement → test → review → security → verify). "
        f"Do NOT substitute built-in tools (EnterPlanMode, ad-hoc Plan agents, improvised flows) "
        f"for workflow-defined phases. The workflow is the single source of truth for orchestration.\n"
        f"4. **Handoff** — Use `.agent-output/handoff-<branch>.md` as defined in "
        f"`{ak_s}/workflows/WORKFLOW_CONTRACT.md`. Generate artifacts in `.agent-output/<role>/` per phase.\n"
        f"5. **No match?** — For trivial tasks (single-file edits, quick questions, config changes), "
        f"proceed without a workflow.\n\n"
    )


def _discovery_recipe(ak_s: str) -> str:
    """Return the universal workflow + skill discovery instructions shared by all agent stubs."""
    return (
        f"{_workflow_recipe(ak_s)}"
        f"## Skill Discovery Protocol\n\n"
        f"This environment uses a centralized AI skills library ([Agent Skills](https://agentskills.io) standard).\n\n"
        f"**On every task**, follow this protocol:\n\n"
        f"1. **Read the catalog** — `{ak_s}/skills-index.json` contains all available skills "
        f"with `id`, `description`, `triggers`, and `path` fields.\n"
        f"2. **Match** — Compare your current task against each skill's `description` and `triggers`.\n"
        f"3. **Load** — For each matching skill, read its full `SKILL.md` at the `path` listed in the index.\n"
        f"4. **Apply** — Follow the skill's instructions. Skill authority overrides generic patterns.\n"
        f"5. **No match?** — Proceed normally without skills.\n\n"
        f"### Key paths\n\n"
        f"| Resource | Path |\n"
        f"|----------|------|\n"
        f"| Skills index | `{ak_s}/skills-index.json` |\n"
        f"| Skills root | `{ak_s}/skills/` |\n"
        f"| Workflows | `{ak_s}/workflows/` |\n"
        f"| Workflow contract | `{ak_s}/workflows/WORKFLOW_CONTRACT.md` |\n"
        f"| Kit docs | `{ak_s}/AGENTS.md` |\n"
    )


def _write_user_file(path: Path, content: str, *, dry_run: bool = False) -> None:
    if dry_run:
        print(f"would write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"Wrote {path}")


def _merge_json_file(path: Path, patch: dict, *, dry_run: bool = False) -> None:
    """Idempotent deep-merge *patch* into an existing JSON file (or create it)."""
    existing: dict = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    changed = False
    for section, entries in patch.items():
        sub = existing.setdefault(section, {})
        for k, v in entries.items():
            if sub.get(k) != v:
                sub[k] = v
                changed = True

    if not changed:
        print(f"json merge: {path} already up to date")
        return
    if dry_run:
        print(f"would update {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Updated {path}")


def _ensure_symlink(link: Path, target: Path, label: str, *, dry_run: bool = False) -> None:
    """Create or verify a symlink. Idempotent."""
    if dry_run:
        print(f"would symlink {link} -> {target} ({label})")
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if link.resolve() == target.resolve():
            print(f"symlink OK: {link} ({label})")
            return
        link.unlink()
    elif link.exists():
        print(f"skip symlink {link}: path exists and is not a symlink ({label})", file=sys.stderr)
        return
    link.symlink_to(target)
    print(f"symlinked {link} -> {target} ({label})")


def _install_claude_plugin(repo: str, name: str, *, dry_run: bool = False) -> None:
    """Install a Claude Code plugin (marketplace add + install). Idempotent / best-effort."""
    for binary in ("engram", "claude"):
        if shutil.which(binary) is None:
            print(f"{name}: '{binary}' not in PATH — skipping", file=sys.stderr)
            return
    if dry_run:
        print(f"would run: claude plugin marketplace add {repo} && claude plugin install {name}")
        return
    for cmd in [
        ["claude", "plugin", "marketplace", "add", repo],
        ["claude", "plugin", "install", name],
    ]:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"{name}: {' '.join(cmd)} failed — {r.stderr.strip()}", file=sys.stderr)
            return
    print(f"{name}: plugin installed")


# --- setup: stub generation -------------------------------------------------------
# Each target entry: (stub_path_relative_to_home, display_title, optional_header)
_STUB_TARGETS: dict[str, tuple[str, str, str]] = {
    "cursor":   (".cursor/AGENT_KIT.md",    "Cursor",        ""),
    "gemini":   (".gemini/GEMINI.md",        "Gemini CLI",    ""),
    "opencode": (".config/opencode/AGENT_KIT.md", "OpenCode", ""),
    "codex":    (".codex/AGENTS.md",         "Codex",         ""),
    "vscode":   (".vscode/copilot-instructions.md", "GitHub Copilot", ""),
    "windsurf": (".codeium/windsurf/memories/global_rules.md", "Windsurf", ""),
    "continue": (".continue/AGENT_KIT.md",   "Continue.dev",  ""),
    "aider":    (".aider/CONVENTIONS.md",     "Aider",         ""),
}


def _stub_header(target: str, ak_s: str) -> str:
    """Return target-specific header lines (empty string for most targets)."""
    if target == "cursor":
        return (
            f"- **Repository:** `{ak_s}` — set `export AGENT_KIT={ak_s}` in shell if needed.\n"
            f"- **Rules:** `{ak_s}/rules/*.mdc` (loaded via alwaysApply / globs)\n"
        )
    if target == "aider":
        return (
            f"Load this file with `/read` at session start, or add to `.aider.conf.yml`:\n"
            f"```yaml\nread:\n  - {Path.home()}/.aider/CONVENTIONS.md\n```\n\n"
        )
    return ""


def _write_stub(target: str, ak_s: str, hint: str, *, extra_footer: str = "", dry_run: bool = False) -> None:
    """Write the discovery-recipe stub for any target."""
    rel_path, title, _ = _STUB_TARGETS[target]
    header = _stub_header(target, ak_s)
    md = (
        f"# ai-resources ({title})\n\n"
        f"{header}"
        f"**Refresh:** {hint}\n\n"
        f"{_discovery_recipe(ak_s)}\n"
        f"{extra_footer}"
    )
    _write_user_file(Path.home() / rel_path, md, dry_run=dry_run)


# --- setup: Claude Code (all-in-one) -----------------------------------------------
_ROLE_TOOLS: dict[str, str | None] = {
    "explore":       "Read, Grep, Glob",
    "planner":       "Read, Grep, Glob, Bash",
    "implementer":   "Read, Edit, Write, Bash, Grep, Glob",
    "tester":        "Read, Edit, Write, Bash, Grep, Glob",
    "code-reviewer": "Read, Bash, Grep, Glob",
    "security-auditor": "Read, Bash, Grep, Glob",
    "software-architect": "Read, Grep, Glob, Bash",
    "verifier":      "Read, Bash, Grep, Glob",
    "generalPurpose": None,
    # Specialists — full tool access (self-contained workflows)
    "crashlytics-fixer":          None,
    "sentry-fixer":               None,
    "package-upgrade":            None,
    "terraform-maintainer":       None,
    "crossplane-upjet-maintainer": None,
}
_MODEL_MAP = {"strong": "opus", "fast": "haiku", "inherit": "inherit"}

# ---------------------------------------------------------------------------
# Agent Team blueprints — pre-defined team compositions for Claude Code.
# These get rendered into the CLAUDE.md stub so the lead agent knows when
# and how to create teams via TeamCreate.
# ---------------------------------------------------------------------------
_TEAM_BLUEPRINTS: list[dict] = [
    {
        "name": "Feature Development",
        "id": "feature",
        "when": (
            "User asks to implement a feature, new functionality, or a multi-step task "
            "that benefits from planning, architecture review, and validation."
        ),
        "lead_role": "You (the lead) coordinate the overall workflow.",
        "teammates": [
            ("planner", "Decomposes the request into PLAN.md with steps and acceptance criteria."),
            ("software-architect", "Validates plan for architecture, separation of concerns, and scalability."),
            ("implementer", "Executes the approved plan with atomic, test-driven edits."),
            ("tester", "Runs the project test suite and analyzes failures."),
            ("code-reviewer", "Reviews changes for standards, security, and maintainability."),
            ("security-auditor", "Scans for vulnerabilities (SAST, secrets, dependencies)."),
            ("verifier", "Checks deliverables against acceptance criteria — only role that marks done."),
        ],
        "workflow": (
            "1. **planner** produces PLAN.md → hands off to **software-architect**\n"
            "2. **software-architect** validates (approve/block) → hands off to **implementer**\n"
            "3. **implementer** codes the plan → hands off to parallel group\n"
            "4. **tester** + **code-reviewer** + **security-auditor** run in parallel\n"
            "5. **verifier** runs after the parallel group completes — marks done or returns gaps"
        ),
        "parallel_groups": "tester, code-reviewer, security-auditor (after implement)",
        "persona_hint": "If the project has a known domain (dart-flutter, symfony, angular, devops), "
                        "teammates should load their domain persona from the personas directory.",
    },
    {
        "name": "Bug Fix",
        "id": "bugfix",
        "when": (
            "User asks to fix a bug, resolve an issue, or correct broken behavior."
        ),
        "lead_role": "You (the lead) triage the bug and coordinate the fix.",
        "teammates": [
            ("explore", "Investigates the codebase to locate the root cause."),
            ("implementer", "Applies the fix with minimal, focused edits."),
            ("tester", "Runs tests to confirm the fix and check for regressions."),
            ("security-auditor", "Verifies the fix doesn't introduce vulnerabilities."),
            ("verifier", "Validates the fix against the reported issue — marks done."),
        ],
        "workflow": (
            "1. **explore** researches the bug, reads code, identifies root cause\n"
            "2. **implementer** applies the fix\n"
            "3. **tester** + **security-auditor** run in parallel\n"
            "4. **verifier** confirms the issue is resolved"
        ),
        "parallel_groups": "tester, security-auditor (after implement)",
    },
    {
        "name": "Security Audit",
        "id": "security",
        "when": (
            "User asks for a security audit, penetration test, vulnerability scan, "
            "or DevSecOps review."
        ),
        "lead_role": "You (the lead) present findings and coordinate remediation.",
        "teammates": [
            ("security-auditor", "Runs the full SAST/DAST/dependency/secrets pipeline."),
            ("code-reviewer", "Reviews code paths flagged by the auditor for exploitability."),
            ("implementer", "Applies remediation patches for confirmed vulnerabilities."),
            ("verifier", "Confirms all critical/high findings are resolved."),
        ],
        "workflow": (
            "1. **security-auditor** runs recon → SAST → dependency scan → secret detection → DAST\n"
            "2. **code-reviewer** reviews flagged code paths in parallel with auditor\n"
            "3. **implementer** applies fixes for confirmed vulnerabilities\n"
            "4. **verifier** validates remediation and produces final report"
        ),
        "parallel_groups": "security-auditor, code-reviewer (initial scan phase)",
    },
    {
        "name": "Production Triage",
        "id": "triage",
        "when": (
            "User asks to fix Crashlytics issues, Sentry errors, or triage production incidents."
        ),
        "lead_role": "You (the lead) prioritize issues and coordinate the response.",
        "teammates": [
            ("sentry-fixer", "Fetches unresolved Sentry issues, analyzes with Seer, applies fixes."),
            ("crashlytics-fixer", "Fetches top Crashlytics crashes, analyzes stack traces, applies fixes."),
            ("tester", "Validates fixes don't introduce regressions."),
            ("verifier", "Confirms issues are resolved in the monitoring platform."),
        ],
        "workflow": (
            "1. **sentry-fixer** and/or **crashlytics-fixer** triage and fix (pick based on platform)\n"
            "2. **tester** runs tests on the affected code paths\n"
            "3. **verifier** confirms the fixes and checks monitoring status"
        ),
        "note": "Spawn only the relevant fixer (sentry-fixer OR crashlytics-fixer) based on the platform.",
    },
    {
        "name": "Infrastructure & Platform",
        "id": "infra",
        "when": (
            "User asks to deploy, change infrastructure, migrate Terraform to Crossplane, "
            "maintain providers, or make changes that span app and infra layers."
        ),
        "lead_role": "You (the lead) coordinate cross-layer changes.",
        "teammates": [
            ("planner", "Plans the infrastructure change with clear steps."),
            ("software-architect", "Validates architecture: module boundaries, state isolation, GitOps."),
            ("crossplane-upjet-maintainer", "Handles Crossplane provider builds, compositions, XRDs, CI/CD."),
            ("terraform-maintainer", "Runs the full Terraform maintenance workflow: audit, upgrade, release."),
            ("security-auditor", "Scans IaC for misconfigurations (tfsec, checkov, kubeconform)."),
            ("verifier", "Validates infra changes against plan and security requirements."),
        ],
        "workflow": (
            "1. **planner** defines the infra change plan\n"
            "2. **software-architect** validates (module boundaries, state, GitOps patterns)\n"
            "3. **crossplane-upjet-maintainer** or **terraform-maintainer** executes (pick based on stack)\n"
            "4. **security-auditor** scans IaC artifacts\n"
            "5. **verifier** confirms all changes are applied and secure"
        ),
        "note": "Spawn the relevant specialist (crossplane or terraform) based on the target stack.",
    },
    {
        "name": "Dependency Maintenance",
        "id": "maintenance",
        "when": (
            "User asks to upgrade dependencies, update packages, or run maintenance across the project."
        ),
        "lead_role": "You (the lead) approve upgrades and coordinate validation.",
        "teammates": [
            ("package-upgrade", "Discovers latest versions, upgrades configs, aligns code with changelogs."),
            ("tester", "Runs the full test suite after upgrades to catch regressions."),
            ("code-reviewer", "Reviews upgrade changes for breaking changes and compatibility."),
        ],
        "workflow": (
            "1. **package-upgrade** runs the full upgrade cycle (discover → upgrade → align code)\n"
            "2. **tester** + **code-reviewer** validate in parallel\n"
            "3. Lead synthesizes results and reports"
        ),
        "parallel_groups": "tester, code-reviewer (after upgrade)",
    },
    {
        "name": "Infrastructure Triage",
        "id": "infra-triage",
        "when": (
            "User reports broken infrastructure, resources not syncing, provider errors, "
            "Crossplane composition failures, CRD issues, or needs to debug and stabilize "
            "IaC resources in a Kubernetes cluster."
        ),
        "lead_role": "You (the lead) coordinate diagnosis and repair.",
        "teammates": [
            ("explore", "Investigates cluster state: claims, XRs, managed resources, providers, CRDs, events."),
            ("software-architect", "Analyzes root cause, proposes fix strategy with explicit blast radius."),
            ("implementer", "Applies fixes to compositions, claims, provider config — one at a time."),
            ("code-reviewer", "Reviews each fix for blast radius: external-name changes, managementPolicies, RBAC."),
            ("verifier", "Monitors until all resources are SYNCED+READY — only role that marks done."),
        ],
        "workflow": (
            "1. **explore** investigates cluster state + IaC code in parallel\n"
            "2. **software-architect** analyzes root cause, proposes fix plan (Observe-first rule)\n"
            "3. **implementer** applies fixes one at a time, waits for Flux between each\n"
            "4. **code-reviewer** + **verifier** validate in parallel (review + monitor)\n"
            "5. **verifier** confirms all resources healthy — marks done"
        ),
        "parallel_groups": "code-reviewer, verifier (after implement)",
        "note": (
            "CRITICAL RULES: (1) Set all affected resources to Observe-only BEFORE any fix. "
            "(2) Never change external-name on resources with existing tfState. "
            "(3) Never delete managed resources unless all have deletionPolicy: Orphan. "
            "(4) Fix one thing at a time — don't batch unrelated fixes."
        ),
    },
    {
        "name": "Code Review",
        "id": "review",
        "when": (
            "User asks for a thorough PR review, pre-merge quality check, or code critique."
        ),
        "lead_role": "You (the lead) synthesize findings into a single review verdict.",
        "teammates": [
            ("code-reviewer", "Reviews standards, performance, maintainability, and correctness."),
            ("security-auditor", "Reviews security implications, injection risks, auth flows."),
        ],
        "workflow": (
            "1. **code-reviewer** + **security-auditor** review in parallel\n"
            "2. Lead consolidates findings into approve / request-changes / block"
        ),
        "parallel_groups": "code-reviewer, security-auditor (full review)",
    },
]


def _build_teams_section(agents_dir: Path) -> str:
    """Build the Agent Teams documentation for the CLAUDE.md stub."""
    names = sorted(p.stem for p in agents_dir.glob("*.md")) if agents_dir.is_dir() else []
    if not names:
        return ""

    # --- Agent table ---
    rows = "| Agent | Use for |\n|-------|--------|\n"
    for n in names:
        try:
            m, _ = _parse_simple_frontmatter(
                (agents_dir / f"{n}.md").read_text(encoding="utf-8", errors="replace")
            )
            d = str(m.get("description", "")).strip('"')[:100]
        except OSError:
            d = ""
        rows += f"| `{n}` | {d} |\n"

    # --- Teams section ---
    teams_md = ""
    for bp in _TEAM_BLUEPRINTS:
        teammates_list = "\n".join(
            f"   - **{name}** — {desc}" for name, desc in bp["teammates"]
        )
        team_block = (
            f"#### {bp['name']} (`{bp['id']}`)\n\n"
            f"**When:** {bp['when']}\n\n"
            f"**Teammates:**\n{teammates_list}\n\n"
            f"**Workflow:**\n{bp['workflow']}\n\n"
        )
        if bp.get("parallel_groups"):
            team_block += f"**Parallel group:** {bp['parallel_groups']}\n\n"
        if bp.get("note"):
            team_block += f"**Note:** {bp['note']}\n\n"
        if bp.get("persona_hint"):
            team_block += f"**Personas:** {bp['persona_hint']}\n\n"
        teams_md += team_block

    return (
        f"## Subagent Definitions (auto-generated)\n\n{rows}\n"
        f"These agents work as **subagents** (via Agent tool) and as **Agent Team teammates** "
        f"(via TeamCreate + SendMessage).\n\n"
        f"### Agent Teams\n\n"
        f"Agent Teams are **enabled automatically** by `kit.py setup`. "
        f"Teams allow multiple agents to work in parallel with direct peer-to-peer "
        f"communication and a shared task list.\n\n"
        f"#### When to use Teams vs Subagents\n\n"
        f"| Use | When |\n"
        f"|-----|------|\n"
        f"| **Subagents** | Quick, focused tasks: single-file exploration, one-shot code review, "
        f"a targeted fix. Sequential — result returns to you. |\n"
        f"| **Agent Teams** | Multi-step workflows with parallel phases: feature development, "
        f"security audits, production triage. Teammates communicate directly and self-coordinate. |\n\n"
        f"#### How to create a team\n\n"
        f"1. Match the user's request to a team blueprint below.\n"
        f"2. Use `TeamCreate` to spawn the team with the listed teammates.\n"
        f"3. Assign initial work via `SendMessage` — include full context "
        f"(teammates don't see your conversation history).\n"
        f"4. Teammates coordinate via the shared task list and direct messages.\n"
        f"5. For **parallel groups**, spawn those teammates simultaneously "
        f"and let them work concurrently.\n"
        f"6. The **verifier** always runs last — it is the only role that can mark work done.\n\n"
        f"#### Team Blueprints\n\n"
        f"{teams_md}"
    )


def _cleanup_stale_hooks(path: Path, *, dry_run: bool = False) -> None:
    """Remove stale workflow-enforcement hooks from settings.json.

    Earlier versions installed a UserPromptSubmit prompt hook that blocked
    trivial messages.  This helper cleans that up so ``setup`` is safe to
    re-run on machines that still carry the old config.
    """
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return
    # Remove only the key we previously owned
    if "UserPromptSubmit" in hooks:
        if dry_run:
            print(f"would remove stale UserPromptSubmit hook from {path}")
            return
        del hooks["UserPromptSubmit"]
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Removed stale UserPromptSubmit hook from {path}")


def _setup_claude(ak: Path, ak_s: str, hint: str, *, dry_run: bool = False) -> None:
    """Full Claude Code setup: settings.json env, subagent definitions, stub, engram, symlinks."""

    # 1. settings.json — env vars (idempotent merge)
    settings_path = Path.home() / ".claude" / "settings.json"
    _merge_json_file(
        settings_path,
        {"env": {
            "AGENT_SKILLS_ROOT": f"{ak_s}/skills",
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
        }},
        dry_run=dry_run,
    )

    # 1b. Clean up stale hooks from earlier versions
    _cleanup_stale_hooks(settings_path, dry_run=dry_run)

    # 2. Subagent definitions from agents/roles/*.md
    roles_dir = ak / "agents" / "roles"
    agents_dir = Path.home() / ".claude" / "agents"
    generated: list[str] = []
    if roles_dir.is_dir():
        for role_md in sorted(roles_dir.glob("*.md")):
            try:
                text = role_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta, body = _parse_simple_frontmatter(text)
            name = str(meta.get("name", role_md.stem))
            desc = str(meta.get("description", ""))
            model = _MODEL_MAP.get(str(meta.get("model", "inherit")), "inherit")
            tools = _ROLE_TOOLS.get(name)

            fm = [f"name: {name}", f'description: "{desc}"']
            if tools is not None:
                fm.append(f"tools: {tools}")
            fm.append(f"model: {model}")

            # Rewrite Cursor-isms → Claude Code equivalents
            for old, new in (
                ("$AGENT_KIT", ak_s),
                ("**the project's `.cursorrules`**", "project CLAUDE.md"),
                ("`.cursorrules`", "CLAUDE.md"),
                (".cursorrules", "CLAUDE.md"),
                ("`@file`", "Read"), ("`@symbol`", "Grep"),
                ("@file", "Read"), ("@symbol", "Grep"),
            ):
                body = body.replace(old, new)

            body += (
                f"\n\n---\n\n## Kit context\n\n"
                f"- **Skills index:** `{ak_s}/skills-index.json`\n"
                f"- **Skills root:** `{ak_s}/skills/`\n"
                f"- **Workflows:** `{ak_s}/workflows/`\n"
            )

            out_path = agents_dir / f"{name}.md"
            _write_user_file(out_path, f"---\n{chr(10).join(fm)}\n---\n\n{body}", dry_run=dry_run)
            generated.append(name)

    if generated and not dry_run:
        print(f"  agents: {', '.join(generated)}")

    # 3. CLAUDE.md stub (with agents table + Agent Teams section)
    agents_footer = _build_teams_section(agents_dir)

    _write_stub("claude", ak_s, hint, extra_footer=agents_footer, dry_run=dry_run)

    # 4. Engram plugin
    _install_claude_plugin("Gentleman-Programming/engram", "engram", dry_run=dry_run)

    # 5. Skill symlinks
    _ensure_symlink(
        Path.home() / ".claude" / "skills" / "ai-resources",
        ak / "skills", "Claude Code", dry_run=dry_run,
    )


# --- setup: dispatch ----------------------------------------------------------------
# Add "claude" to _STUB_TARGETS so _write_stub can resolve its path/title.
_STUB_TARGETS["claude"] = (".claude/CLAUDE.md", "Claude Code", "")


def cmd_setup(args: argparse.Namespace) -> int:
    ak = _default_agent_kit()
    ak_s = str(ak)
    hint = _kit_refresh_hint(ak)
    target = (args.target or "all").strip().lower()
    dry = getattr(args, "dry_run", False)
    targets_done: list[str] = []

    # MCP presets from resources.json
    if target in ("all", "cursor", "mcp"):
        mcp_t = "all" if target in ("all", "mcp") else "cursor"
        rc = _apply_mcp_config(argparse.Namespace(target=mcp_t, dry_run=dry))
        if rc != 0:
            return rc
        targets_done.append("mcp")

    def _run_target(name: str) -> None:
        if name == "claude":
            _setup_claude(ak, ak_s, hint, dry_run=dry)
        else:
            _write_stub(name, ak_s, hint, dry_run=dry)
        targets_done.append(name)

    if target == "all":
        for name in _STUB_TARGETS:
            _run_target(name)
        # Codex skill symlinks (Claude symlinks handled inside _setup_claude)
        _ensure_symlink(
            Path.home() / ".agents" / "skills" / "ai-resources",
            ak / "skills", "Codex", dry_run=dry,
        )
        targets_done.append("symlinks")
    elif target == "mcp":
        pass
    elif target in _STUB_TARGETS:
        _run_target(target)
        if target == "codex":
            _ensure_symlink(
                Path.home() / ".agents" / "skills" / "ai-resources",
                ak / "skills", "Codex", dry_run=dry,
            )
            targets_done.append("symlinks")
    else:
        print(f"Unknown target: {target!r}", file=sys.stderr)
        return 1

    if not getattr(args, "skip_workflow_check", False):
        rc = _check_workflows()
        if rc != 0:
            return rc

    if getattr(args, "fix_workflow_ids", False):
        _patch_workflow_ids()
        targets_done.append("fix-workflow-ids")

    if not dry:
        _write_setup_state(ak, sorted(set(targets_done)))

    return 0


# --- setup: workflow validation (internal) -----------------------------------------
def _resolve_skill_id(skill_id: str, skills_root: Path) -> Path | None:
    if not skill_id or skill_id.startswith("#"):
        return None
    if "/" in skill_id:
        return None
    p = skills_root / skill_id / "SKILL.md"
    return p if p.is_file() else None


def _extract_skills_from_workflow_text(text: str) -> list[str]:
    out: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if re.match(r"^\s*skills:\s*$", lines[i]) or re.match(r"^\s*skills:\s*\[\s*\]\s*$", lines[i]):
            i += 1
            while i < len(lines):
                raw = lines[i]
                if re.match(r"^\s+-\s+", raw):
                    m = re.match(r"^\s+-\s+(.+?)\s*$", raw)
                    if m:
                        val = m.group(1).strip().strip("\"'")
                        if val and not val.startswith("#"):
                            out.append(val)
                    i += 1
                    continue
                if raw.strip() == "" or raw.strip().startswith("#"):
                    i += 1
                    continue
                if re.match(r"^\s{4}[a-zA-Z_][a-zA-Z0-9_]*:", raw):
                    break
                if raw.strip() and not raw.startswith(" "):
                    break
                i += 1
            continue
        i += 1
    return out


def _check_workflows() -> int:
    ak = _default_agent_kit()
    skills_root = Path(os.environ.get("AGENT_SKILLS_ROOT", ak / "skills"))
    wf_dir = ak / "workflows"
    if not wf_dir.is_dir():
        print(f"No workflows dir: {wf_dir}", file=sys.stderr)
        return 1

    errors: list[str] = []
    for wf in sorted(wf_dir.glob("*.workflow.yaml")):
        text = wf.read_text(encoding="utf-8", errors="replace")
        for sid in _extract_skills_from_workflow_text(text):
            resolved = _resolve_skill_id(sid, skills_root)
            if resolved is None:
                errors.append(f"{wf.name}: skill id not found: {sid!r}")

    if errors:
        print("setup (workflow validation): FAILED", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"setup (workflow validation): OK ({wf_dir})")
    return 0


# --- setup: patch workflow skill ids (internal) ------------------------------------
def _patch_workflow_ids() -> int:
    wf = REPO / "workflows"
    for path in sorted(wf.glob("*.workflow.yaml")):
        text = path.read_text(encoding="utf-8")
        out_lines = []
        in_skills = False
        for line in text.splitlines(keepends=True):
            if re.match(r"^\s*skills:\s*$", line):
                in_skills = True
                out_lines.append(line)
                continue
            if in_skills:
                m = re.match(r"^(\s+-\s+)(.+?)(\s*)$", line.rstrip("\n"))
                if m:
                    prefix, val, trail = m.group(1), m.group(2).strip(), m.group(3)
                    if val and not val.startswith("#") and "/" in val:
                        new = workflow_skill_id_to_flat(val)
                        line = f"{prefix}{new}{trail}\n"
                    out_lines.append(line)
                    continue
                if line.strip() and not line.startswith(" ") and not line.startswith("\t"):
                    in_skills = False
                elif re.match(r"^\s{4}[a-zA-Z_][a-zA-Z0-9_]*:", line):
                    in_skills = False
                out_lines.append(line)
                continue
            out_lines.append(line)
        path.write_text("".join(out_lines), encoding="utf-8")
        print(path.name)
    return 0


# --- CLI ---------------------------------------------------------------------------
_EPILOG = """Typical flows:
  Import vendors + rebuild skills-index.json (default):
    python3 scripts/kit.py generate
    python3 scripts/kit.py generate --dry-run   # preview vendor import; index still rebuilt from disk

  Index only (no git imports from resources.json):
    python3 scripts/kit.py generate --skip-vendor

  After updating the kit (Homebrew or git pull):
    brew upgrade <formula>   # or: git -C $AGENT_KIT pull
    python3 scripts/kit.py generate

  Machine + IDE + validate workflows (writes ~/.config/ai-resources/state.json):
    python3 scripts/kit.py setup                       # all agents + MCP + symlinks
    python3 scripts/kit.py setup --target cursor       # Cursor only
    python3 scripts/kit.py setup --target claude       # Claude Code + engram + symlinks
    python3 scripts/kit.py setup --target windsurf     # Windsurf only
    python3 scripts/kit.py setup --fix-workflow-ids    # optional: rewrite workflow skill ids in-place

  Supported targets: cursor, claude, gemini, opencode, codex, vscode, windsurf, continue, aider, mcp

Environment (optional):
  AGENT_KIT          Repo root (default: parent of scripts/)
  AGENT_SKILLS_ROOT  Default: $AGENT_KIT/skills
  SKILLS_INDEX_OUT   Default: $AGENT_KIT/skills-index.json
"""


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="kit.py",
        description=(
            "ai-resources kit: `generate` imports vendor skills and builds skills-index.json; "
            "`setup` applies MCP + IDE stubs, validates workflows, optional workflow id rewrite."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOG,
    )
    sub = ap.add_subparsers(
        dest="command",
        metavar="COMMAND",
        title="commands",
        description="Run python3 scripts/kit.py COMMAND --help for command-specific options.",
    )

    def _add_vendor_sync_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--dry-run", action="store_true", help="Print actions without writing")
        p.add_argument(
            "--force",
            action="store_true",
            help="Overwrite dirs managed by the configured source or resolve vendor meta conflicts",
        )

    p_gen = sub.add_parser(
        "generate",
        help="Import vendor skills (resources.json) then build skills-index.json; or index-only with --skip-vendor.",
        description=(
            "Default: imports git sources from resources.json into skills/, then walks AGENT_SKILLS_ROOT, "
            "reads every SKILL.md frontmatter, and writes skills-index.json (or SKILLS_INDEX_OUT). "
            "Pass --skip-vendor to only rebuild the index. "
            "Use --dry-run / --force on the import step."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_vendor_sync_flags(p_gen)
    p_gen.add_argument(
        "--skip-vendor",
        action="store_true",
        help="Only rebuild skills-index.json from the current skills/ tree (skip git imports)",
    )
    p_gen.set_defaults(func=cmd_generate)

    p_st = sub.add_parser(
        "setup",
        help="MCP presets, IDE stubs, validate workflows; optional rewrite workflow skill ids.",
        description=(
            "Applies mcp.* from resources.json (e.g. ~/.cursor/mcp.json), writes per-IDE pointer files under ~, "
            "then validates workflows/*.workflow.yaml against skills/. "
            "Records ~/.config/ai-resources/state.json (agent_kit path + targets; skipped with --dry-run). "
            "Use --fix-workflow-ids to rewrite slash-style skill ids in workflows (in-place)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_st.add_argument(
        "--target",
        choices=["all", "cursor", "claude", "gemini", "opencode", "codex", "vscode",
                 "windsurf", "continue", "aider", "mcp"],
        default="all",
        help="Bootstrap scope (default: all)",
    )
    p_st.add_argument("--dry-run", action="store_true", help="Preview MCP write only")
    p_st.add_argument(
        "--skip-workflow-check",
        action="store_true",
        help="Skip validating workflow skill ids against skills/",
    )
    p_st.add_argument(
        "--fix-workflow-ids",
        action="store_true",
        help="Rewrite slash-style skill ids in workflows/*.workflow.yaml (in-place)",
    )
    p_st.set_defaults(func=cmd_setup)

    sub.add_parser("version", help="Print the current ai-resources version.")

    args = ap.parse_args()
    if not args.command:
        ap.print_help()
        return 1
    if args.command == "version":
        print(f"ai-resources {__version__}")
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
