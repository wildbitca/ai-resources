"""Skills index generation + vendor sync (preserved from kit.py v0.7.x)."""
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

from . import repo_root

META_NAME = ".skill-source.yaml"


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


def _resources_path() -> Path:
    return repo_root() / "resources.json"


def read_resources() -> dict:
    path = _resources_path()
    if not path.is_file():
        raise FileNotFoundError(f"Missing resources manifest: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _frontmatter_raw(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    return text[3:end].strip()


def parse_simple_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Parse YAML-ish frontmatter. Used kit-wide; kept here so other modules import it."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end].strip()
    meta: dict[str, object] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("- "):
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


def _extract_triggers(meta: dict[str, object], body: str, block: str = "") -> list[str]:
    out: list[str] = []
    desc = str(meta.get("description", "") or "")
    if "Trigger:" in desc:
        out.append(desc.split("Trigger:", 1)[1].strip()[:500])
    for source in (desc, block, body[:5000]):
        if not source:
            continue
        m = re.search(r"\(triggers?:\s*([^)]+)\)", source, re.I)
        if m:
            out.append(m.group(1).strip()[:500])
            break
    return out


def _build_index() -> int:
    ak = repo_root()
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
        block = _frontmatter_raw(text)
        meta, body = parse_simple_frontmatter(text)

        rel_parent = skill_md.parent.relative_to(root).as_posix()
        sid = rel_parent
        name = str(meta.get("name", rel_parent.replace("/", "-")))
        desc = str(meta.get("description", ""))[:2000]

        triggers_list = _parse_list_field(block, "triggers") if block else []
        if not triggers_list and meta.get("triggers"):
            triggers_list = [str(meta["triggers"])[:500]]
        triggers = triggers_list or _extract_triggers(meta, body, block or "")

        globs_list = _parse_list_field(block, "globs") if block else []
        if not globs_list and meta.get("globs"):
            globs_list = [str(meta["globs"])[:500]]

        entries.append({
            "id": sid,
            "path": str(skill_md),
            "name": name,
            "description": desc,
            "triggers": triggers,
            "globs": globs_list,
        })

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


def _normalize_config(data: dict) -> dict:
    if "skills" not in data:
        raise ValueError("resources.json must contain a top-level 'skills' object")
    out = dict(data)
    out.setdefault("agents", {"sources": []})
    out.setdefault("personas", {"sources": []})
    out["skills"].setdefault("sources", [])
    return out


def _git_clone(url: str, ref: str, depth: int, dest: Path) -> None:
    subprocess.run(
        ["git", "clone", "--depth", str(depth), "--branch", ref, url, str(dest)],
        check=True, capture_output=True, text=True,
    )


def _git_head(clone: Path) -> str:
    r = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True)
    return (r.stdout or "").strip() or "unknown"


def _skill_id_for(prefix: str, rel: Path) -> str:
    return f"{prefix}-{rel.as_posix().replace('/', '-')}"


def _write_meta(dest: Path, source_id: str, from_rel: str, revision: str) -> None:
    body = (
        f"# Auto-generated by ai-resources generate (vendor import)\n"
        f"source_id: {source_id}\n"
        f"from_path: {from_rel}\n"
        f"git_revision: {revision}\n"
    )
    (dest / META_NAME).write_text(body, encoding="utf-8")


def _sync_source(src: dict, skills_root: Path, dry_run: bool, force: bool) -> list[str]:
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
        _git_clone(url, ref, depth, clone)
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
                            errors.append(f"CONFLICT: {dest.name} managed by different source (expected {sid})")
                            continue
                    else:
                        if not sk_id.startswith(managed_prefix):
                            errors.append(f"CONFLICT: {dest.name} blocks import (not {managed_prefix}*, no {META_NAME})")
                            continue
                        if not force:
                            errors.append(f"CONFLICT: {dest.name} exists without {META_NAME}; use --force to replace from {sid}")
                            continue

                if dry_run:
                    print(f"would sync {from_rel} -> skills/{sk_id}/ @ {rev[:8]}")
                    continue

                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(skill_md.parent, dest)
                _write_meta(dest, sid, from_rel, rev)
                print(f"synced {sk_id} <= {from_rel} ({rev[:8]})")

    return errors


def _import_skills(args: argparse.Namespace) -> int:
    cfg_path = _resources_path()
    if not cfg_path.is_file():
        print(f"Missing {cfg_path}", file=sys.stderr)
        return 1

    try:
        cfg = _normalize_config(json.loads(cfg_path.read_text(encoding="utf-8")))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    skills_root = Path(os.environ.get("AGENT_SKILLS_ROOT", repo_root() / "skills"))
    os.environ.setdefault("AGENT_SKILLS_ROOT", str(skills_root))

    if cfg.get("agents", {}).get("sources") or cfg.get("personas", {}).get("sources"):
        print("Note: agents/personas sources not yet implemented — only skills are processed.", file=sys.stderr)

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


def cmd_generate(args: argparse.Namespace) -> int:
    """Vendor-sync (unless --skip-vendor) then rebuild skills-index.json."""
    if not getattr(args, "skip_vendor", False):
        rc = _import_skills(args)
        if rc != 0:
            return rc
        print("==> skills-index.json", file=sys.stderr)
    return _build_index()
