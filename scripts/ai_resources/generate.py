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
    """Parse frontmatter as YAML, falling back to a line parser.

    YAML first because the line parser cannot read block scalars: `description: >` gave back
    the literal ">", which left every skill written that way with no usable description in
    `skills-index.json`. The fallback keeps malformed third-party frontmatter importable.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end].strip()
    body_text = text[end + 4:].lstrip()

    try:
        import yaml  # local import: the CLI runs without PyYAML for other commands
        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict):
            return {k: v for k, v in parsed.items() if k is not None}, body_text
    except Exception:  # noqa: BLE001 — malformed frontmatter falls back to the line parser
        pass

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
        desc = " ".join(str(meta.get("description", "")).split())[:2000]

        def _as_list(value: object) -> list[str]:
            if isinstance(value, (list, tuple)):
                return [str(v)[:500] for v in value]
            return [str(value)[:500]] if value else []

        triggers_list = _parse_list_field(block, "triggers") if block else []
        if not triggers_list and meta.get("triggers"):
            triggers_list = _as_list(meta["triggers"])
        triggers = triggers_list or _extract_triggers(meta, body, block or "")

        globs_list = _parse_list_field(block, "globs") if block else []
        if not globs_list and meta.get("globs"):
            globs_list = _as_list(meta["globs"])

        try:
            path_str = skill_md.relative_to(ak).as_posix()  # relative to the kit root: portable
        except ValueError:
            path_str = str(skill_md)
        entries.append({
            "id": sid,
            "path": path_str,
            "name": name,
            "description": desc,
            "triggers": triggers,
            "globs": globs_list,
        })

    try:
        skills_root = root.relative_to(ak).as_posix()
    except ValueError:
        skills_root = str(root.resolve())
    doc = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "skills_root": skills_root,
        "count": len(entries),
        "skills": entries,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} ({len(entries)} skills)")
    return 0


WORKFLOW_SKILL_MARKER = ".generated-workflow-skill"

# Workflows that also ship as dynamic workflow scripts (installed as /kit-* commands by setup).
WORKFLOW_SCRIPTS: dict[str, str] = {
    "feature-implementation": "`/kit-plan <goal>` then `/kit-implement`",
    "bugfix": "`/kit-plan` with kind \"bugfix\", then `/kit-implement`",
    "refactor": "`/kit-plan` with kind \"refactor\", then `/kit-implement`",
}


def _workflow_skill_md(filename: str, doc: dict) -> str:
    name = str(doc["name"])
    trigger = " ".join(str(doc.get("trigger", "")).split())
    summary = " ".join(str(doc.get("description", "")).split())
    description = f"{trigger} Runs the kit workflow '{name}': {summary}".replace('"', "'")[:1000]
    rows = ["| Step | Agent | Parallel group |", "|------|-------|----------------|"]
    for step in doc.get("steps") or []:
        group = (step.get("execution_hints") or {}).get("parallel_group") or "—"
        rows.append(f"| `{step.get('id')}` | `{step.get('subagent_type')}` | {group} |")
    table = "\n".join(rows)
    script = WORKFLOW_SCRIPTS.get(name)
    deterministic = (
        f"\n## Deterministic alternative\n\n"
        f"{script} runs this loop as workflow scripts: one writer, a test gate, parallel review lenses "
        f"and findings verified before they are fixed. Prefer it unless a step needs judgement mid-run "
        f"(workflow scripts cannot ask the user anything until they finish).\n"
        if script else ""
    )
    return (
        "---\n"
        f"name: workflow-{name}\n"
        f'description: "{description}"\n'
        'argument-hint: "[goal]"\n'
        "---\n\n"
        f"<!-- Generated from workflows/{filename} by `ai-resources generate`. Edit the YAML, not this file. -->\n\n"
        f"# Workflow: {name}\n\n"
        f"{summary}\n\n"
        "**Goal:** $ARGUMENTS\n\n"
        "1. Load the `kit-orchestration` skill: it defines how to run steps, the handoff file, "
        "parallel groups, and the subagent return format.\n"
        f"2. Read the full definition at `$AGENT_KIT/workflows/{filename}` (`kit-orchestration` "
        "explains how to locate `$AGENT_KIT`) and run its steps in order.\n\n"
        "## Steps\n\n"
        f"{table}\n"
        f"{deterministic}"
    )


def _build_workflow_skills() -> int:
    """Generate one `workflow-<name>` skill per workflows/*.workflow.yaml.

    Agents discover skills natively by description, so each workflow becomes a skill whose
    description is the workflow's trigger. Stale generated skills are removed; hand-written
    skills (no marker file) are never touched.
    """
    try:
        import yaml
    except ImportError:
        print("PyYAML is required to generate workflow skills", file=sys.stderr)
        return 1
    ak = repo_root()
    skills_root = ak / "skills"
    expected: set[str] = set()
    for wf in sorted((ak / "workflows").glob("*.workflow.yaml")):
        doc = yaml.safe_load(wf.read_text(encoding="utf-8")) or {}
        if not doc.get("name"):
            print(f"skip {wf.name}: no name", file=sys.stderr)
            continue
        skill_id = f"workflow-{doc['name']}"
        expected.add(skill_id)
        dest = skills_root / skill_id
        if dest.is_dir() and not (dest / WORKFLOW_SKILL_MARKER).is_file():
            print(f"CONFLICT: skills/{skill_id} exists and is not a generated workflow skill", file=sys.stderr)
            return 2
        dest.mkdir(parents=True, exist_ok=True)
        (dest / WORKFLOW_SKILL_MARKER).write_text(f"source: workflows/{wf.name}\n", encoding="utf-8")
        content = _workflow_skill_md(wf.name, doc)
        skill_md = dest / "SKILL.md"
        if not skill_md.is_file() or skill_md.read_text(encoding="utf-8") != content:
            skill_md.write_text(content, encoding="utf-8")
            print(f"workflow skill {skill_id} <= workflows/{wf.name}")
    for dest in sorted(skills_root.glob("workflow-*")):
        if dest.name not in expected and (dest / WORKFLOW_SKILL_MARKER).is_file():
            shutil.rmtree(dest)
            print(f"removed stale workflow skill {dest.name}")
    return 0


def _normalize_config(data: dict) -> dict:
    if "skills" not in data:
        raise ValueError("resources.json must contain a top-level 'skills' object")
    out = dict(data)
    out.setdefault("agents", {"sources": []})
    out.setdefault("personas", {"sources": []})
    out["skills"].setdefault("sources", [])
    return out


def _git_clone(url: str, ref: str, depth: int, dest: Path, sha: str = "") -> None:
    """Clone `ref`, then check out `sha` when the source pins one.

    A pinned sha is what makes an import reproducible: these files are prompts that run in
    the user's agent, so the revision must change only when someone reviews the diff.
    """
    subprocess.run(
        ["git", "clone", "--depth", str(depth), "--branch", ref, url, str(dest)],
        check=True, capture_output=True, text=True,
    )
    if sha:
        fetch = subprocess.run(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", sha],
                               capture_output=True, text=True)
        if fetch.returncode != 0:  # shallow fetch by sha is not always allowed; deepen instead
            subprocess.run(["git", "-C", str(dest), "fetch", "--unshallow"],
                           capture_output=True, text=True)
        checkout = subprocess.run(["git", "-C", str(dest), "checkout", "--quiet", sha],
                                  capture_output=True, text=True)
        if checkout.returncode != 0:
            raise RuntimeError(
                f"pinned sha {sha[:8]} is not reachable from {ref}: "
                f"{(checkout.stderr or '').strip().splitlines()[-1] if checkout.stderr else 'checkout failed'}"
            )


def _git_head(clone: Path) -> str:
    r = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True)
    return (r.stdout or "").strip() or "unknown"


def _skill_id_for(prefix: str, rel: Path) -> str:
    return f"{prefix}-{rel.as_posix().replace('/', '-')}"


def _normalize_skill_name(skill_md: Path, skill_id: str) -> None:
    """Set the frontmatter `name` to the directory id.

    Agents derive a skill's identity from its directory, and the Agent Skills spec requires the
    two to match; upstream names like `typescript` would collide once vendored as `gpm-*`.
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return
    if not text.startswith("---"):
        skill_md.write_text(f"---\nname: {skill_id}\n---\n\n{text}", encoding="utf-8")
        return
    end = text.find("\n---", 3)
    if end == -1:
        return
    # `body` keeps the newline that ends the closing `---` line, so it is appended as-is:
    # adding another one inserts a blank line into every skill on every import.
    block, body = text[3:end], text[end + 4:]
    if re.search(r"^name:\s*.*$", block, re.MULTILINE):
        block = re.sub(r"^name:\s*.*$", f"name: {skill_id}", block, count=1, flags=re.MULTILINE)
    else:
        block = f"\nname: {skill_id}{block}"
    skill_md.write_text(f"---{block}\n---{body}", encoding="utf-8")


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

    pinned = str(src.get("sha", "") or "")

    with tempfile.TemporaryDirectory(prefix=f"skill-src-{sid}-") as tmp:
        clone = Path(tmp) / "src"
        try:
            _git_clone(url, ref, depth, clone, pinned)
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            errors.append(f"{sid}: {exc}")
            return errors
        rev = _git_head(clone)
        if pinned and rev != pinned:
            errors.append(f"{sid}: checked out {rev[:8]}, expected pinned {pinned[:8]}")
            return errors
        if not pinned:
            print(f"WARNING: source '{sid}' is not pinned to a sha; imported {rev[:8]} from {ref}",
                  file=sys.stderr)

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
                _normalize_skill_name(dest / "SKILL.md", sk_id)
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


def _sync_plugin_agents() -> None:
    """Keep `.claude-plugin/plugin.json` agents in step with agents/roles/.

    The plugin schema takes a list of agent files, not a directory, so the list has to be
    enumerated — and would rot silently every time a role is added or removed.
    """
    manifest_path = repo_root() / ".claude-plugin" / "plugin.json"
    roles_dir = repo_root() / "agents" / "roles"
    if not manifest_path.is_file() or not roles_dir.is_dir():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"WARNING: {manifest_path.name} is not valid JSON ({exc}); agents not synced",
              file=sys.stderr)
        return
    agents = [f"./agents/roles/{p.name}" for p in sorted(roles_dir.glob("*.md"))]
    if manifest.get("agents") == agents:
        return
    manifest["agents"] = agents
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    print(f"plugin.json: agents synced ({len(agents)} roles)")


def cmd_generate(args: argparse.Namespace) -> int:
    """Vendor-sync (unless --skip-vendor), generate workflow skills, rebuild skills-index.json."""
    if not getattr(args, "skip_vendor", False):
        rc = _import_skills(args)
        if rc != 0:
            return rc
    print("==> workflow skills", file=sys.stderr)
    rc = _build_workflow_skills()
    if rc != 0:
        return rc
    _sync_plugin_agents()
    print("==> skills-index.json", file=sys.stderr)
    return _build_index()
