#!/usr/bin/env python3
"""
Generate skills-index.json for dynamic discovery (paths, ids, triggers heuristic).
Stdlib only — minimal YAML-like frontmatter between --- lines.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_simple_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end].strip()
    meta: dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([a-zA-Z0-9_-]+):\s*(.*)$", line)
        if m:
            k, v = m.group(1), m.group(2).strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            meta[k] = v
    body = text[end + 4 :].lstrip()
    return meta, body


def extract_triggers(meta: dict[str, str], body: str) -> list[str]:
    out: list[str] = []
    desc = meta.get("description", "")
    if "Trigger:" in desc:
        part = desc.split("Trigger:", 1)[1].strip()
        out.append(part[:500])
    m = re.search(r"\(triggers?:\s*([^)]+)\)", body[:5000], re.I)
    if m:
        out.append(m.group(1).strip()[:500])
    return out


def extract_globs(body: str) -> list[str]:
    globs: list[str] = []
    for m in re.finditer(r"\*\*([^*]+\*[^*]*)\*\*", body[:8000]):
        globs.append(m.group(1).strip())
    return globs[:20]


def _default_agent_kit() -> Path:
    env = os.environ.get("AGENT_KIT")
    if env:
        return Path(env)
    # scripts/ -> repo root
    return Path(__file__).resolve().parents[1]


def main() -> int:
    ak = _default_agent_kit()
    root = Path(os.environ.get("AGENT_SKILLS_ROOT", ak / "cursor" / "skills"))
    if not root.is_dir():
        print(f"Skills root not found: {root}", file=sys.stderr)
        return 1

    out_path = Path(os.environ.get("SKILLS_INDEX_OUT", ak / "cursor" / "skills-index.json"))

    entries: list[dict] = []
    for skill_md in sorted(root.rglob("SKILL.md")):
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, body = parse_simple_frontmatter(text)
        rel_parent = skill_md.parent.relative_to(root).as_posix()
        sid = rel_parent
        name = meta.get("name", rel_parent.replace("/", "-"))
        desc = meta.get("description", "")[:2000]
        entries.append(
            {
                "id": sid,
                "path": str(skill_md),
                "name": name,
                "description": desc,
                "triggers": extract_triggers(meta, body),
                "globs": extract_globs(body),
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
    gen = ak / "skills-index.generated.json"
    try:
        gen.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Copied to {gen}")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
