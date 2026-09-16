"""Shared helpers for cockpit configurators."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import state, ui


def deep_merge_json(path: Path, patch: dict, *, dry_run: bool = False) -> bool:
    """Merge patch into JSON at path. Returns True if written."""
    existing: dict = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            ui.warn(f"{path} is not valid JSON — left unchanged; fix it and re-run setup.")
            return False
        if not isinstance(existing, dict):
            ui.warn(f"{path} is not a JSON object — left unchanged.")
            return False

    def _merge(dst: dict, src: dict) -> bool:
        changed = False
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                if _merge(dst[k], v):
                    changed = True
            elif dst.get(k) != v:
                dst[k] = v
                changed = True
        return changed

    changed = _merge(existing, patch)
    if not changed:
        return False
    if dry_run:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def env_keys_added_by_patch(path: Path, patch_env: dict[str, str]) -> list[str]:
    """Return env keys from patch_env that aren't already present at path['env'].

    Used by cockpit configurators to record exactly which keys they're introducing
    (so we can later remove only those, not user-set values).
    """
    if not path.is_file():
        return list(patch_env.keys())
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return list(patch_env.keys())
    existing_env = existing.get("env") or {}
    return [k for k in patch_env if k not in existing_env]


def remove_env_keys_from_settings(path: Path, keys: list[str]) -> list[str]:
    """Remove the given keys from the settings.json `env` block. Returns keys actually removed."""
    if not path.is_file() or not keys:
        return []
    try:
        cur = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    env_block = cur.get("env")
    if not isinstance(env_block, dict):
        return []
    removed = [k for k in keys if k in env_block]
    for k in removed:
        env_block.pop(k, None)
    if not env_block:
        cur.pop("env", None)
    path.write_text(json.dumps(cur, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return removed


def write_text(path: Path, content: str, *, dry_run: bool = False) -> bool:
    """Write text file, creating parent dirs. Returns True if written/changed."""
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8")
            if existing == content:
                return False
        except OSError:
            pass
    if dry_run:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


LEGACY_SKILLS_LINK = "ai-resources"


def stable_kit_root(root: Path) -> Path:
    """Map a versioned Homebrew Cellar path to its version-independent opt/ path.

    `.../Cellar/ai-resources/1.1.9/libexec` → `.../opt/ai-resources/libexec`, so links
    written by setup keep resolving after `brew upgrade`. Non-Homebrew roots are returned as-is.
    """
    parts = root.parts
    if "Cellar" not in parts:
        return root
    i = parts.index("Cellar")
    if len(parts) < i + 3:
        return root
    opt = Path(*parts[:i], "opt", parts[i + 1], *parts[i + 3:])
    return opt if opt.exists() else root


def _link_target(link: Path) -> Path:
    """Lexically normalized target of a symlink (the target may not exist)."""
    raw = os.readlink(link)
    return Path(os.path.normpath(raw if os.path.isabs(raw) else os.path.join(link.parent, raw)))


def _is_kit_skills_dir(d: Path, skills_root: Path) -> bool:
    """True for the current kit skills dir or a Homebrew install of this kit (any version)."""
    if d == skills_root:
        return True
    p = d.parts
    return p[-2:] == ("libexec", "skills") and (
        p[-5:-3] == ("Cellar", "ai-resources") or p[-4:-2] == ("opt", "ai-resources")
    )


def sync_skill_links(links_dir: Path, skills_root: Path, *, dry_run: bool = False) -> dict[str, list[str]]:
    """Link every kit skill as `links_dir/<skill-id>` → `skills_root/<skill-id>`.

    Agents discover skills one directory level below their skills dir, so a single link to
    the whole kit (`links_dir/ai-resources`) hides every skill. This replaces that legacy link,
    prunes kit links whose skill no longer exists, and never touches the user's own skills:
    a name already taken by a real directory or a foreign link is reported as skipped.
    """
    result: dict[str, list[str]] = {"linked": [], "removed": [], "skipped": []}
    skills_root = Path(os.path.normpath(skills_root))
    if not skills_root.is_dir():
        return result
    wanted = {d.name: d for d in sorted(skills_root.iterdir()) if (d / "SKILL.md").is_file()}

    if links_dir.is_dir():
        for entry in sorted(links_dir.iterdir()):
            if not entry.is_symlink():
                continue
            target = _link_target(entry)
            legacy = entry.name == LEGACY_SKILLS_LINK and _is_kit_skills_dir(target, skills_root)
            kit_link = _is_kit_skills_dir(target.parent, skills_root)
            if legacy or (kit_link and wanted.get(entry.name) != target):
                if not dry_run:
                    entry.unlink()
                result["removed"].append(entry.name)

    for name, src in wanted.items():
        link = links_dir / name
        if name not in result["removed"] and (link.is_symlink() or link.exists()):
            if link.is_symlink() and _link_target(link) == src:
                continue
            result["skipped"].append(name)
            continue
        if not dry_run:
            links_dir.mkdir(parents=True, exist_ok=True)
            link.symlink_to(src, target_is_directory=True)
        result["linked"].append(name)
    return result


def ensure_symlink(link: Path, target: Path, *, dry_run: bool = False) -> bool:
    if dry_run:
        return False
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if link.resolve() == target.resolve():
            return False
        link.unlink()
    elif link.exists():
        return False
    link.symlink_to(target)
    return True


MANAGED_BEGIN = ("<!-- BEGIN ai-resources: managed by `ai-resources setup`; "
                 "edits inside this block are overwritten -->")
MANAGED_END = "<!-- END ai-resources -->"
_BEGIN_LINE = re.compile(r"^<!-- BEGIN ai-resources\b.*-->\s*$")
_END_LINE = re.compile(r"^<!-- END ai-resources -->\s*$")
_KIT_TITLE = "# ai-resources ("
_LEGACY_TITLE = re.compile(r"^# ai-resources \(", re.MULTILINE)

# `## ` headings written under the `# ai-resources (<tool>)` title by kit versions up to 1.1.x.
LEGACY_KIT_HEADINGS = frozenset({
    "Multi-Model Routing Protocol (ai-resources)",
    "Workflow Discovery Protocol (MANDATORY — checked FIRST)",
    "Skill Discovery Protocol",
    "Skill Discovery",
    "Subagent Definitions (auto-generated)",
    "Memory (Engram MCP)",
})
# `## ` headings of the current block; used to clean up a block whose END marker was deleted.
CURRENT_KIT_HEADINGS = frozenset({"Using the kit", "Delegation", "Multi-model routing"})


def _is_fence(bare: str) -> bool:
    return bare.lstrip().startswith(("```", "~~~"))


def strip_legacy_kit_sections(text: str, headings: frozenset[str] = LEGACY_KIT_HEADINGS) -> str:
    """Remove kit sections written without markers, keeping the user's content.

    Only text under a `# ai-resources (<tool>)` title is considered: the title's own lines and
    each `## ` section whose heading is in `headings`, up to the next `#`/`##` heading outside a
    code fence. A same-named section under any other `# ` heading is the user's and is kept.
    """
    kept: list[str] = []
    skipping = in_fence = under_kit_title = False
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        if not in_fence:
            if bare.startswith("# "):
                under_kit_title = bare.startswith(_KIT_TITLE)
                skipping = under_kit_title
            elif bare.startswith("## "):
                skipping = under_kit_title and bare[3:].strip() in headings
        if _is_fence(bare):
            in_fence = not in_fence
        if not skipping:
            kept.append(line)
    return "".join(kept).strip("\r\n")


def _managed_block_span(lines: list[str]) -> tuple[int | None, int | None]:
    """Indexes of the BEGIN and END marker lines, matched as whole lines outside code fences."""
    in_fence = False
    begin: int | None = None
    for i, line in enumerate(lines):
        bare = line.rstrip("\r\n")
        if _is_fence(bare):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if begin is None and _BEGIN_LINE.match(bare):
            begin = i
        elif begin is not None and _END_LINE.match(bare):
            return begin, i
    return begin, None


def write_managed_block(path: Path, body: str, *, dry_run: bool = False) -> bool:
    """Write `body` between the kit markers in `path`, preserving everything outside them.

    With markers present only the block is replaced. Otherwise the block is prepended and kit
    text written without markers (by kit versions up to 1.1.x, or a block whose END marker was
    deleted) is removed. Whenever text outside the block changes, the original is saved as
    `<name>.ai-resources-backup-<timestamp>` and the user is told. Line endings are preserved.
    Returns True if the file changed.
    """
    raw = ""
    if path.is_file():
        with open(path, encoding="utf-8", newline="") as fh:
            raw = fh.read()
    nl = "\r\n" if "\r\n" in raw else "\n"
    block = nl.join([MANAGED_BEGIN, *body.strip().splitlines(), MANAGED_END]) + nl
    lines = raw.splitlines(keepends=True)
    begin, end = _managed_block_span(lines)
    if begin is not None and end is not None:
        updated = "".join(lines[:begin]) + block + "".join(lines[end + 1:])
        outside_changed = False
    else:
        rest = raw
        if begin is not None:  # BEGIN without END: drop the orphan marker and the kit text after it
            rest = "".join(lines[:begin] + lines[begin + 1:])
            rest = strip_legacy_kit_sections(rest, LEGACY_KIT_HEADINGS | CURRENT_KIT_HEADINGS)
        elif _LEGACY_TITLE.search(rest):
            rest = strip_legacy_kit_sections(rest)
        rest = rest.strip("\r\n")
        updated = block + (f"{nl}{rest}{nl}" if rest else "")
        outside_changed = rest != raw.strip("\r\n")
    if updated == raw or dry_run:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if outside_changed and raw.strip():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.ai-resources-backup-{stamp}")
        with open(backup, "w", encoding="utf-8", newline="") as fh:
            fh.write(raw)
        ui.warn(f"{path}: old kit sections moved into a managed block; original saved as {backup.name}")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(updated)
    return True


def merge_kit_hooks(path: Path, kit_hooks: dict[str, list[dict]],
                    is_kit_command: Callable[[str], bool], *, dry_run: bool = False) -> bool:
    """Install the kit's hook entries in a settings.json, replacing only earlier kit entries.

    `deep_merge_json` replaces lists wholesale, which would drop the user's own hooks, so
    hooks are merged per event: entries whose command `is_kit_command` are replaced and
    every other entry is kept in place. Returns True if the file changed.
    """
    settings: Any = {}
    if path.is_file():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            ui.warn(f"{path} is not valid JSON — kit hooks not installed.")
            return False
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict):
        ui.warn(f"{path} has an unexpected shape — kit hooks not installed.")
        return False
    hooks = settings.get("hooks", {})

    def without_kit_hooks(entry: Any) -> Any:
        """The entry minus kit commands; None when nothing but kit commands was in it."""
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list) or not entry["hooks"]:
            return entry
        kept = [h for h in entry["hooks"]
                if not (isinstance(h, dict) and is_kit_command(str(h.get("command", ""))))]
        if not kept:
            return None
        return entry if len(kept) == len(entry["hooks"]) else {**entry, "hooks": kept}

    merged: dict[str, Any] = {}
    for event in list(hooks) + [e for e in kit_hooks if e not in hooks]:
        current = hooks.get(event, [])
        if not isinstance(current, list):
            merged[event] = current
            continue
        entries = [e for e in map(without_kit_hooks, current) if e is not None]
        entries += list(kit_hooks.get(event, []))
        if entries:
            merged[event] = entries
    if merged == hooks or dry_run:
        return False
    if merged:
        settings["hooks"] = merged
    else:
        settings.pop("hooks", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def link_agents_skills(ak_path: str) -> None:
    """Link kit skills into ~/.agents/skills, the shared Agent Skills location."""
    agents_skills = Path.home() / ".agents" / "skills"
    links = sync_skill_links(agents_skills, Path(ak_path) / "skills")
    if links["removed"]:
        ui.info(f"Removed outdated kit skill links: {', '.join(links['removed'])}")
    if links["skipped"]:
        ui.warn(f"Skills not linked (name already used in {agents_skills}): "
                f"{', '.join(links['skipped'])}")


def multimodel_protocol_md(ak_path: str, gateway_url: str, mode: str = "multi-model") -> str:
    """Return the multi-model section for instruction files (empty in single-model mode).

    The backend is read off the gateway URL rather than passed in: nine call sites
    across the cockpits already hand us the URL, and threading a parallel `backend`
    argument through all of them would let the two drift apart.
    """
    if mode == "single-model":
        return ""
    backend = "openrouter" if "openrouter.ai" in gateway_url else "litellm"
    name = "OpenRouter" if backend == "openrouter" else "LiteLLM gateway"
    cost = (
        "- Claude Code prices every figure it shows (`/usage`, the status line, `--max-budget-usd`) "
        "at Anthropic list price, so under OpenRouter those numbers are wrong. The spend limit on "
        "the OpenRouter key is the real ceiling.\n"
        if backend == "openrouter" else ""
    )
    return (
        "## Multi-model routing\n\n"
        f"Model calls go through {name} at `{gateway_url}`. Each kit subagent's `model:` "
        "is routed to its provider; the role → model map is `~/.config/ai-resources/executors.yaml` "
        "(`ai-resources executors show` / `set`).\n\n"
        "- Anthropic does not support routing Claude Code to non-Claude models; check the gateway "
        "with `ai-resources doctor`.\n"
        "- Prompt caching is lost on non-Anthropic routes, so a cheaper model can cost more per task. "
        "Keep review, security and verification on strong models.\n"
        f"{cost}"
    )


def kit_instructions_md(tool: str, ak_path: str, gateway_url: str, mode: str, *,
                        native_skills: bool, extra: str = "") -> str:
    """Kit block for the instruction files of cockpits other than Claude Code."""
    if native_skills:
        skills = ("- **Skills** are linked into `~/.agents/skills/`, which this tool discovers natively. "
                  "Load a skill when its description matches the task; a loaded skill overrides "
                  "generic habits.\n")
    else:
        skills = (f"- **Skills:** search `{ak_path}/skills-index.json` for skills whose description "
                  "matches the task, then read only those `SKILL.md` files (paths are relative to "
                  "the kit root).\n")
    return (
        f"# ai-resources ({tool})\n\n"
        f"Kit root: `{ak_path}`. After `brew upgrade ai-resources`, run `ai-resources setup`.\n\n"
        f"{extra}"
        "## Using the kit\n\n"
        f"{skills}"
        "- **Workflows** for multi-step work are the `workflow-*` skills; the `kit-orchestration` "
        f"skill explains how to run them (definitions in `{ak_path}/workflows/`).\n\n"
        f"{multimodel_protocol_md(ak_path, gateway_url, mode)}"
    )


def kit_context_block(ak_path: str) -> str:
    """Return the standard kit-context footer used in subagent files."""
    return (
        f"\n\n---\n\n## Kit context\n\n"
        f"- **Kit root (`$AGENT_KIT`):** `{ak_path}` — skills, workflows, `handoff.md.template`.\n"
        f"- **In a workflow step:** read the skills named in your prompt, update the handoff file "
        f"(unless your step runs in a parallel group), and end with the return block from the "
        f"`kit-orchestration` skill.\n"
    )


def mcp_engram_block() -> dict:
    """Standard Engram MCP server block (matches Claude Desktop / Code / Gemini schema)."""
    return {
        "engram": {
            "command": "engram",
            "args": ["mcp"],
        }
    }
