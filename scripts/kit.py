#!/usr/bin/env python3
"""
ai-resources kit CLI — two commands: generate (skills + index), setup (machine + IDE + validation).

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


def _extract_triggers_from_description(meta: dict[str, object], body: str) -> list[str]:
    out: list[str] = []
    desc = str(meta.get("description", "") or "")
    if "Trigger:" in desc:
        part = desc.split("Trigger:", 1)[1].strip()
        out.append(part[:500])
    if "(triggers:" in desc:
        m = re.search(r"\(triggers?:\s*([^)]+)\)", desc, re.I)
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
        triggers = triggers_list if triggers_list else _extract_triggers_from_description(meta, body)
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


def _write_user_file(path: Path, content: str, *, dry_run: bool = False) -> None:
    if dry_run:
        print(f"would write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"Wrote {path}")


def _write_cursor_stub(ak_s: str, hint: str, *, dry_run: bool = False) -> None:
    md = (
        f"# ai-resources\n\n"
        f"- **Repository:** `{ak_s}` — set `export AGENT_KIT={ak_s}` in shell if needed.\n"
        f"- **Layout, CLI, env:** see `{ak_s}/README.md`\n"
        f"- **Refresh:** {hint}\n"
    )
    _write_user_file(Path.home() / ".cursor" / "AGENT_KIT.md", md, dry_run=dry_run)


def _write_claude_stub(ak_s: str, hint: str, *, dry_run: bool = False) -> None:
    md = (
        f"# ai-resources (Claude Code)\n\n"
        f"1. **Repo:** `{ak_s}`\n"
        f"2. **Skills:** `{ak_s}/skills` — set `export AGENT_SKILLS_ROOT={ak_s}/skills`\n"
        f"3. **Index:** `{ak_s}/skills-index.json`\n\n"
        f"{hint}\n"
    )
    _write_user_file(Path.home() / ".claude" / "CLAUDE.md", md, dry_run=dry_run)


def _write_gemini_stub(ak_s: str, *, dry_run: bool = False) -> None:
    md = (
        f"# ai-resources (Gemini)\n\n"
        f"Repo: `{ak_s}` — Skills: `{ak_s}/skills` — Index: `{ak_s}/skills-index.json`\n"
    )
    _write_user_file(Path.home() / ".gemini" / "GEMINI.md", md, dry_run=dry_run)


def _write_opencode_stub(ak_s: str, *, dry_run: bool = False) -> None:
    md = (
        f"# ai-resources (OpenCode)\n\n"
        f"Repo: `{ak_s}` — `{ak_s}/skills-index.json` — details: `{ak_s}/README.md`\n"
    )
    _write_user_file(Path.home() / ".config" / "opencode" / "AGENT_KIT.md", md, dry_run=dry_run)


def _write_codex_stub(ak_s: str, *, dry_run: bool = False) -> None:
    md = f"# ai-resources (Codex)\n\nRepo: `{ak_s}` — Discovery: `{ak_s}/skills-index.json`\n"
    _write_user_file(Path.home() / ".codex" / "AGENTS.md", md, dry_run=dry_run)


def _write_vscode_stub(ak_s: str, *, dry_run: bool = False) -> None:
    md = f"# ai-resources (Copilot)\n\nSkills: `{ak_s}/skills` — `{ak_s}/README.md`\n"
    _write_user_file(Path.home() / ".vscode" / "copilot-instructions.md", md, dry_run=dry_run)


def cmd_setup(args: argparse.Namespace) -> int:
    ak = _default_agent_kit()
    ak_s = str(ak)
    hint = _kit_refresh_hint(ak)
    target = (args.target or "all").strip().lower()
    dry = getattr(args, "dry_run", False)
    targets_done: list[str] = []

    # MCP presets from resources.json (cursor is the only implemented target today; "all" uses same block)
    if target in ("all", "cursor", "mcp"):
        mcp_t = "all" if target in ("all", "mcp") else "cursor"
        rc = _apply_mcp_config(argparse.Namespace(target=mcp_t, dry_run=dry))
        if rc != 0:
            return rc
        targets_done.append("mcp")

    if target == "all":
        _write_cursor_stub(ak_s, hint, dry_run=dry)
        _write_claude_stub(ak_s, hint, dry_run=dry)
        _write_gemini_stub(ak_s, dry_run=dry)
        _write_opencode_stub(ak_s, dry_run=dry)
        _write_codex_stub(ak_s, dry_run=dry)
        _write_vscode_stub(ak_s, dry_run=dry)
        targets_done.extend(["cursor", "claude", "gemini", "opencode", "codex", "vscode"])
    elif target == "cursor":
        _write_cursor_stub(ak_s, hint, dry_run=dry)
        targets_done.append("cursor")
    elif target == "claude":
        _write_claude_stub(ak_s, hint, dry_run=dry)
        targets_done.append("claude")
    elif target == "gemini":
        _write_gemini_stub(ak_s, dry_run=dry)
        targets_done.append("gemini")
    elif target == "opencode":
        _write_opencode_stub(ak_s, dry_run=dry)
        targets_done.append("opencode")
    elif target == "codex":
        _write_codex_stub(ak_s, dry_run=dry)
        targets_done.append("codex")
    elif target == "vscode":
        _write_vscode_stub(ak_s, dry_run=dry)
        targets_done.append("vscode")
    elif target == "mcp":
        pass  # MCP already applied above
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
    python3 scripts/kit.py setup
    python3 scripts/kit.py setup --target cursor
    python3 scripts/kit.py setup --fix-workflow-ids   # optional: rewrite workflow skill ids in-place

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
        choices=["all", "cursor", "claude", "gemini", "opencode", "codex", "vscode", "mcp"],
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

    args = ap.parse_args()
    if not args.command:
        ap.print_help()
        return 1
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
