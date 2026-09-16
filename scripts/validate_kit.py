#!/usr/bin/env python3
"""Validate the kit's content: skills, workflows, agents, hooks and generated artifacts.

Runs without network or API access, so CI can gate every pull request on it. Behavioural
testing of the prompts themselves is `claude plugin eval` (see evals/), not this script.

Usage:
    python3 scripts/validate_kit.py [--fix-index]

Exit code 0 when everything passes, 1 when any check fails.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs it
    print("PyYAML is required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(1)

ROOT = Path(__file__).resolve().parents[1]
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DESCRIPTION_LIMIT = 1024  # Claude Code truncates description + when_to_use at 1536 chars
VERIFIER_DOC_SKILL = "knowledge-audit"

failures: list[str] = []


def fail(check: str, message: str) -> None:
    failures.append(f"[{check}] {message}")


def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError as exc:
        fail("frontmatter", f"{path.relative_to(ROOT)}: invalid YAML ({exc.__class__.__name__})")
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        # A scalar or list document parses fine but has no fields; without this the callers
        # would raise AttributeError and the run would end in a traceback, not a finding.
        fail("frontmatter", f"{path.relative_to(ROOT)}: frontmatter is {type(data).__name__}, expected a mapping")
        return {}
    return data


def check_skills() -> set[str]:
    """Every skill dir has a valid SKILL.md whose name matches the directory."""
    ids: set[str] = set()
    skills_root = ROOT / "skills"
    if not skills_root.is_dir():
        fail("skills", "skills/ does not exist")
        return ids
    for skill_dir in sorted(skills_root.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("_"):
            continue
        rel = skill_dir.relative_to(ROOT)
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            fail("skills", f"{rel}: no SKILL.md")
            continue
        ids.add(skill_dir.name)
        meta = frontmatter(skill_md)
        name = str(meta.get("name", ""))
        description = str(meta.get("description", ""))
        if not SKILL_NAME_RE.match(skill_dir.name):
            fail("skills", f"{rel}: directory name is not kebab-case")
        if name != skill_dir.name:
            fail("skills", f"{rel}: frontmatter name '{name}' != directory name")
        if not description.strip():
            fail("skills", f"{rel}: no description — the agent cannot know when to load it")
        elif len(description) > DESCRIPTION_LIMIT:
            fail("skills", f"{rel}: description is {len(description)} chars (limit {DESCRIPTION_LIMIT})")
    return ids


def check_workflows(skill_ids: set[str], role_names: set[str]) -> None:
    """Steps reference real roles and skills; routing targets resolve; invariants hold."""
    for wf_path in sorted((ROOT / "workflows").glob("*.workflow.yaml")):
        rel = wf_path.relative_to(ROOT)
        try:
            doc = yaml.safe_load(wf_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            fail("workflows", f"{rel}: invalid YAML ({exc})")
            continue
        for key in ("name", "description", "trigger", "steps"):
            if not doc.get(key):
                fail("workflows", f"{rel}: missing top-level '{key}'")
        steps = doc.get("steps") or []
        ids = {s.get("id") for s in steps}
        groups: dict[str, list[dict]] = {}
        for step in steps:
            sid = step.get("id", "?")
            where = f"{rel}:{sid}"
            if step.get("subagent_type") not in role_names:
                fail("workflows", f"{where}: unknown subagent_type '{step.get('subagent_type')}'")
            for skill in step.get("skills") or []:
                if skill not in skill_ids:
                    fail("workflows", f"{where}: unknown skill '{skill}'")
            for field in ("handoff_to", "on_concern_return_to"):
                target = step.get(field)
                if target not in (None, *ids):
                    fail("workflows", f"{where}: {field} '{target}' is not a step in this workflow")
            # The verifier checks acceptance criteria against evidence; documenting is a separate step.
            if step.get("subagent_type") == "verifier":
                if VERIFIER_DOC_SKILL in (step.get("skills") or []):
                    fail("workflows", f"{where}: verifier must not own '{VERIFIER_DOC_SKILL}'")
                if "SDD closure" in str(step.get("prompt_template", "")):
                    fail("workflows", f"{where}: verifier must not run SDD closure — use a document step")
            group = (step.get("execution_hints") or {}).get("parallel_group")
            if group:
                groups.setdefault(group, []).append(step)
        for group, members in groups.items():
            where = f"{rel}:{group}"
            if len({m.get("entry_criteria") for m in members}) > 1:
                fail("workflows", f"{where}: steps in a parallel group need the same entry criteria")
            for member in members:
                if "Update {{handoff_file}}" in str(member.get("prompt_template", "")):
                    fail("workflows", f"{where}:{member.get('id')}: parallel steps must not write the handoff")


def check_roles() -> set[str]:
    names: set[str] = set()
    for role_path in sorted((ROOT / "agents" / "roles").glob("*.md")):
        meta = frontmatter(role_path)
        name = str(meta.get("name", ""))
        if name != role_path.stem:
            fail("roles", f"{role_path.relative_to(ROOT)}: frontmatter name '{name}' != file name")
        if not str(meta.get("description", "")).strip():
            fail("roles", f"{role_path.relative_to(ROOT)}: no description")
        names.add(role_path.stem)
    return names


def check_personas(role_names: set[str]) -> int:
    """Every persona must name a real role, or it generates nothing at all.

    Personas are `<role>-<domain>.md` and the setup step resolves the role by
    longest prefix match. A typo in the role half matches nothing, so the file
    is skipped silently: no subagent, no warning, no failing command. This turns
    that into a build error.
    """
    personas_dir = ROOT / "agents" / "personas"
    if not personas_dir.is_dir():
        return 0
    count = 0
    for persona_path in sorted(personas_dir.glob("*.md")):
        name = persona_path.stem
        if not any(name.startswith(f"{role}-") for role in role_names):
            fail("personas", f"{persona_path.relative_to(ROOT)}: no role matches the '<role>-<domain>' prefix")
            continue
        meta = frontmatter(persona_path)
        if str(meta.get("name", "")) != name:
            fail("personas", f"{persona_path.relative_to(ROOT)}: frontmatter name '{meta.get('name','')}' != file name")
        if not str(meta.get("description", "")).strip():
            fail("personas", f"{persona_path.relative_to(ROOT)}: no description")
        count += 1
    return count


def check_workflow_scripts() -> None:
    """Dynamic workflow scripts must satisfy the runtime's rules."""
    forbidden = ("Date.now(", "Math.random(", "new Date(", "import(", "require(", "process.")
    for script in sorted((ROOT / "workflows" / "scripts").glob("*.js")):
        rel = script.relative_to(ROOT)
        text = script.read_text(encoding="utf-8")
        if not text.startswith("export const meta = {"):
            fail("scripts", f"{rel}: 'export const meta' must be the first statement")
            continue
        meta_block = text[: text.index("\n}\n") + 3]
        for token in forbidden:
            if token in text:
                fail("scripts", f"{rel}: uses {token} — unavailable in the workflow runtime")
        if re.search(r"\$\{|\.\.\.", meta_block.split("phases:")[0]):
            fail("scripts", f"{rel}: meta must be a pure literal (no interpolation or spreads)")
        declared = set(re.findall(r"title: '([^']+)'", meta_block))
        used = set(re.findall(r"phase\('([^']+)'\)", text[len(meta_block):]))
        for title in sorted(used - declared):
            fail("scripts", f"{rel}: phase('{title}') has no entry in meta.phases")


def check_hooks() -> None:
    for hook in sorted((ROOT / "hooks").glob("*.py")):
        result = subprocess.run([sys.executable, "-m", "py_compile", str(hook)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            fail("hooks", f"{hook.relative_to(ROOT)}: does not compile\n{result.stderr.strip()}")


def check_references() -> None:
    """Paths and rule/skill references named in the kit's own docs must exist."""
    patterns = (
        (re.compile(r"\$AGENT_KIT/([A-Za-z0-9_./-]+)"), lambda m: m.group(1)),
        (re.compile(r"`rules/([A-Za-z0-9_.-]+\.mdc)`"), lambda m: f"rules/{m.group(1)}"),
    )
    targets = list((ROOT / "workflows").glob("*.yaml")) + list((ROOT / "rules").glob("*.mdc"))
    targets += [ROOT / "AGENTS.md", ROOT / "README.md", ROOT / "handoff.md.template"]
    targets += list((ROOT / "skills" / "kit-orchestration").rglob("*.md"))
    for doc in targets:
        if not doc.is_file():
            continue
        text = doc.read_text(encoding="utf-8")
        for pattern, extract in patterns:
            for match in pattern.finditer(text):
                ref = extract(match).rstrip(".,:;)")
                if "{" in ref or "*" in ref or ref.endswith("/"):
                    continue
                if not (ROOT / ref).exists():
                    fail("references", f"{doc.relative_to(ROOT)}: '{ref}' does not exist")


def check_index(skill_ids: set[str]) -> None:
    """skills-index.json must match what is on disk, with kit-relative paths.

    Compared field by field rather than by regenerating: running `generate` here would
    rewrite generated workflow skills as a side effect of a read-only check.
    """
    index_path = ROOT / "skills-index.json"
    if not index_path.is_file():
        fail("index", "skills-index.json is missing — run `ai-resources generate`")
        return
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail("index", f"skills-index.json is not valid JSON ({exc})")
        return

    stale = "run `ai-resources generate` (with AGENT_SKILLS_ROOT unset) and commit the result"
    entries = {entry["id"]: entry for entry in data.get("skills", [])}
    missing = sorted(skill_ids - set(entries))
    extra = sorted(set(entries) - skill_ids)
    if missing:
        fail("index", f"not indexed: {', '.join(missing[:5])} — {stale}")
    if extra:
        fail("index", f"indexed but gone from disk: {', '.join(extra[:5])} — {stale}")

    for skill_id, entry in sorted(entries.items()):
        if Path(entry.get("path", "")).is_absolute():
            fail("index", f"{skill_id}: index paths must be relative to the kit root — {stale}")
            break
    def normalized(value: object) -> str:
        # The index collapses whitespace, so a `description: >` block keeps its meaning but
        # not its line breaks; compare the same way rather than flagging every block scalar.
        return " ".join(str(value).split())

    drifted: list[str] = []
    for skill_id in sorted(skill_ids & set(entries)):
        on_disk = frontmatter(ROOT / "skills" / skill_id / "SKILL.md")
        entry = entries[skill_id]
        if normalized(on_disk.get("description", "")) != normalized(entry.get("description", "")):
            drifted.append(f"{skill_id} (description)")
        elif str(on_disk.get("name", skill_id)) != entry.get("name", ""):
            drifted.append(f"{skill_id} (name)")
    if drifted:
        fail("index", f"{len(drifted)} entr(ies) differ from their SKILL.md "
                      f"({', '.join(drifted[:5])}) — {stale}")


def check_plugin_manifest(role_names: set[str]) -> None:
    """The plugin manifest must list every role file and point at paths that exist.

    `agents` takes files, not a directory, so the list rots as roles come and go;
    `ai-resources generate` rewrites it and this check keeps it honest.
    """
    manifest_path = ROOT / ".claude-plugin" / "plugin.json"
    if not manifest_path.is_file():
        fail("plugin", ".claude-plugin/plugin.json is missing")
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail("plugin", f"plugin.json is not valid JSON ({exc})")
        return

    # Sort by file name, exactly as `generate.py` does: sorting stems would order
    # `doc.md` before `doc-writer.md` and leave the two permanently disagreeing.
    expected = [f"./agents/roles/{p.name}" for p in sorted((ROOT / "agents" / "roles").glob("*.md"))]
    if manifest.get("agents") != expected:
        fail("plugin", "plugin.json agents is out of step with agents/roles/ — run `ai-resources generate`")
    for key in ("agents", "workflows", "hooks", "skills"):
        value = manifest.get(key)
        for path in ([value] if isinstance(value, str) else value or []):
            if isinstance(path, str) and not (ROOT / path).exists():
                fail("plugin", f"plugin.json {key}: '{path}' does not exist")

    marketplace_path = ROOT / ".claude-plugin" / "marketplace.json"
    if not marketplace_path.is_file():
        fail("plugin", ".claude-plugin/marketplace.json is missing")
        return
    try:
        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail("plugin", f"marketplace.json is not valid JSON ({exc})")
        return
    names = {entry.get("name") for entry in marketplace.get("plugins", [])}
    if manifest.get("name") not in names:
        fail("plugin", f"marketplace.json does not list the plugin '{manifest.get('name')}'")


def check_vendored_provenance() -> None:
    """Every vendored skill records where it came from, and the source is pinned."""
    manifest = json.loads((ROOT / "resources.json").read_text(encoding="utf-8"))
    for source in manifest.get("skills", {}).get("sources", []):
        if not source.get("sha"):
            fail("supply-chain",
                 f"resources.json: source '{source.get('id')}' is not pinned to a commit "
                 f"(add \"sha\"); ref '{source.get('ref')}' can move under us")
    for meta_path in sorted((ROOT / "skills").glob("*/.skill-source.yaml")):
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        if not meta.get("git_revision") or meta.get("git_revision") == "unknown":
            fail("supply-chain", f"{meta_path.relative_to(ROOT)}: no git_revision recorded")


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()

    skill_ids = check_skills()
    role_names = check_roles()
    persona_count = check_personas(role_names)
    check_workflows(skill_ids, role_names)
    check_workflow_scripts()
    check_hooks()
    check_references()
    check_plugin_manifest(role_names)
    check_vendored_provenance()
    check_index(skill_ids)

    if failures:
        print(f"{len(failures)} problem(s):\n", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"kit OK — {len(skill_ids)} skills, {len(role_names)} roles, "
          f"{persona_count} personas, "
          f"{len(list((ROOT / 'workflows').glob('*.workflow.yaml')))} workflows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
