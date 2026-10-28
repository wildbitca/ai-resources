"""Shared helpers for cockpit configurators."""
from __future__ import annotations

import json
import os
import re
import stat
from ... import scratch as _scratch
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import state, ui


class SkipCockpit(Exception):
    """A cockpit cannot be given what the selection asks for; the wizard prints the reason and moves on."""


def deep_merge_json(path: Path, patch: dict, *, dry_run: bool = False, mode: int | None = None) -> bool:
    """Merge patch into JSON at path. Returns True if written.

    With `mode` (0o600 for a file that will carry a key) the file is created with it and an existing,
    wider file is tightened to it even when the merge changes nothing.
    """
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
        if mode is not None and not dry_run and path.is_file() and path.stat().st_mode & 0o777 != mode:
            path.chmod(mode)
        return False
    if dry_run:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(existing, indent=2, ensure_ascii=False) + "\n"
    if mode is None:
        path.write_text(text, encoding="utf-8")
    else:
        _write_with_mode(path, text, mode)
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


def _write_with_mode(path: Path, content: str, mode: int) -> None:
    """Write `content` to `path` created with `mode` from the start (no window at the umask default);
    an existing file is tightened to it as well, because O_CREAT does not reset the mode."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(content)
    finally:
        if fd >= 0:
            os.close(fd)


def write_text(path: Path, content: str, *, dry_run: bool = False, mode: int | None = None) -> bool:
    """Write text file, creating parent dirs. Returns True if written/changed.

    With `mode`, the file is created with that mode from the start and an existing, wider file is
    tightened to it, even when its content is unchanged: use 0o600 for files that carry a key.
    """
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8")
            if existing == content:
                if mode is not None and path.stat().st_mode & 0o777 != mode:
                    path.chmod(mode)
                return False
        except OSError:
            pass
    if dry_run:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        path.write_text(content, encoding="utf-8")
    else:
        _write_with_mode(path, content, mode)
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
    """True for the current kit skills dir, a Homebrew install of this kit (any version), or the skills dir of an
    ai-resources checkout (a developer's earlier links point there; setup moves them to the installed kit)."""
    if d == skills_root:
        return True
    p = d.parts
    if p[-2:] == ("libexec", "skills") and (
        p[-5:-3] == ("Cellar", "ai-resources") or p[-4:-2] == ("opt", "ai-resources")
    ):
        return True
    return d.name == "skills" and (d.parent / "skills-index.json").is_file() and (
        d.parent / "scripts" / "ai_resources").is_dir()


def sync_skill_links(links_dir: Path, skills_root: Path, *, dry_run: bool = False,
                     include: "set[str] | None" = None) -> dict[str, list[str]]:
    """Link every kit skill as `links_dir/<skill-id>` → `skills_root/<skill-id>`.

    Agents discover skills one directory level below their skills dir, so a single link to
    the whole kit (`links_dir/ai-resources`) hides every skill. This replaces that legacy link,
    prunes kit links whose skill no longer exists, and never touches the user's own skills:
    a name already taken by a real directory or a foreign link is reported as skipped.

    `include` limits the linked skills to those ids (None = every skill, the historical behaviour).
    A kit link whose skill is excluded is pruned like one whose skill is gone; foreign links are never touched.
    """
    result: dict[str, list[str]] = {"linked": [], "removed": [], "skipped": []}
    skills_root = Path(os.path.normpath(skills_root))
    if not skills_root.is_dir():
        return result
    wanted = {d.name: d for d in sorted(skills_root.iterdir()) if (d / "SKILL.md").is_file()}
    if include is not None:
        wanted = {n: d for n, d in wanted.items() if n in include}

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
# Every renderer that feeds the managed pair contributes here: the common block, the multi-model
# protocol, and the OpenClaw engine part (`Orchestration`, `Voice`, `Memory`, `Voice notes`). They
# are only ever stripped under a `# ai-resources (<tool>)` title, so a user's own section of the
# same name under another H1 is kept.
CURRENT_KIT_HEADINGS = frozenset({
    "Using the kit", "Delegation", "Waiting", "Waiting without polling", "Multi-model routing", "Kit workflow", "Worktrees",
    "Long-running commands never block a tool call", "Scratch space", "Memory", "Voice notes", "Orchestration", "Voice",
})


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


def managed_block_pairs(path: Path) -> tuple[int, bool]:
    """(complete BEGIN/END pairs in `path`, whether a BEGIN with no END follows them). Read-only.

    Built on `_managed_block_span`, so the verifier and the writer can never disagree about what
    a marker is. A missing file is (0, False).
    """
    lines = _read_raw(path).splitlines(keepends=True)
    pairs = 0
    while lines:
        begin, end = _managed_block_span(lines)
        if begin is None:
            return pairs, False
        if end is None:
            return pairs, True
        pairs += 1
        lines = lines[end + 1:]
    return pairs, False


def _render_managed(raw: str, body: str) -> tuple[str, bool]:
    """(`raw` with `body` in its managed block, whether text OUTSIDE the block changed)."""
    nl = "\r\n" if "\r\n" in raw else "\n"
    block = nl.join([MANAGED_BEGIN, *body.strip().splitlines(), MANAGED_END]) + nl
    lines = raw.splitlines(keepends=True)
    begin, end = _managed_block_span(lines)
    if begin is not None and end is not None:
        return "".join(lines[:begin]) + block + "".join(lines[end + 1:]), False
    rest = raw
    if begin is None and not _LEGACY_TITLE.search(rest):
        # Nothing of the kit's in the file: prepend the block and keep every other byte (only the
        # leading blank lines go, so removing the block later gives the file back).
        rest = raw.lstrip("\r\n")
        return block + (f"{nl}{rest}" if rest else ""), False
    if begin is not None:  # BEGIN without END: drop the orphan marker and the kit text after it
        rest = "".join(lines[:begin] + lines[begin + 1:])
        rest = strip_legacy_kit_sections(rest, LEGACY_KIT_HEADINGS | CURRENT_KIT_HEADINGS)
    else:
        rest = strip_legacy_kit_sections(rest)
    rest = rest.strip("\r\n")
    return block + (f"{nl}{rest}{nl}" if rest else ""), rest != raw.strip("\r\n")


def _read_raw(path: Path) -> str:
    if not path.is_file():
        return ""
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def managed_block_would_change(path: Path, body: str) -> bool:
    """Whether `write_managed_block(path, body)` would change the file (it writes nothing)."""
    raw = _read_raw(path)
    return _render_managed(raw, body)[0] != raw


def _write_target(path: Path) -> Path | None:
    """The file a write to `path` has to replace. A symlink (a dotfile manager) is followed to its target, so the
    link survives and the target changes; a link whose target directory does not exist is left alone with one
    line (None)."""
    if not path.is_symlink():
        return path
    target = Path(os.path.realpath(path))
    if not target.parent.is_dir():
        ui.warn(f"{path} is a symlink to {target}, whose directory does not exist; left alone")
        return None
    return target


def _replace_atomically(target: Path, text: str) -> None:
    """Write `text` to a temp file next to `target` (same mode as `target`, line endings untouched) and move it
    over: a reader sees the old file or the new one, never half of it, and an interrupted write changes nothing."""
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except OSError:
        mode = 0o644
    fd, tmp = _scratch.atomic_tmp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    from .. import state as _state
    _state.remove_stale_temp_files(target)         # a SIGKILLed earlier write leaves `.AGENTS.md.*.tmp`


def _create_backup(target: Path, raw: str, stamp: str) -> Path:
    """Save `raw` as `<name>.ai-resources-backup-<stamp>`, created exclusively (never through a planted link and
    never over an existing file: the next free `-N` suffix is used)."""
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except OSError:
        mode = 0o600
    for n in range(100):
        backup = target.with_name(f"{target.name}.ai-resources-backup-{stamp}" + (f"-{n}" if n else ""))
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(raw)
        return backup
    raise OSError(f"no free backup name for {target}")


def remove_managed_block(path: Path) -> bool:
    """Drop the managed block from `path`, leaving every other byte as it was. True if it changed."""
    if not path.is_file():
        return False
    raw = _read_raw(path)
    lines = raw.splitlines(keepends=True)
    begin, end = _managed_block_span(lines)
    if begin is None or end is None:
        return False
    rest = "".join(lines[:begin] + lines[end + 1:])
    # write_managed_block puts one blank line between the block and what it prepended to.
    nl = "\r\n" if "\r\n" in raw else "\n"
    if begin == 0 and rest.startswith(nl):
        rest = rest[len(nl):]
    target = _write_target(path)
    if target is None:
        return False
    _replace_atomically(target, rest)
    return True


def write_managed_block(path: Path, body: str, *, dry_run: bool = False) -> bool:
    """Write `body` between the kit markers in `path`, preserving everything outside them.

    With markers present only the block is replaced. Otherwise the block is prepended and kit
    text written without markers (by kit versions up to 1.1.x, or a block whose END marker was
    deleted) is removed. Whenever text outside the block changes, the original is saved as
    `<name>.ai-resources-backup-<timestamp>` and the user is told. Line endings are preserved.
    The write is atomic (temp file + replace), keeps the file mode and goes through a symlink to its target.
    Returns True if the file changed.
    """
    raw = _read_raw(path)
    updated, outside_changed = _render_managed(raw, body)
    if updated == raw or dry_run:
        return False
    target = _write_target(path)
    if target is None:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if outside_changed and raw.strip():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = _create_backup(target, raw, stamp)
        ui.warn(f"{path}: old kit sections moved into a managed block; original saved as {backup.name}")
    _replace_atomically(target, updated)
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


def _kit_skill_ids(skills_root: Path) -> list[str]:
    return [d.name for d in skills_root.iterdir() if (d / "SKILL.md").is_file()] if skills_root.is_dir() else []


def link_agents_skills(ak_path: str, cockpit: str = "agents") -> None:
    """Link the scoped kit skills into ~/.agents/skills, the shared Agent Skills location (ADR-0015 S9: the same
    `skill_scope` rule as Claude Code; AI_RESOURCES_SKILLS=all restores every skill)."""
    from ... import skill_scope
    agents_skills = Path.home() / ".agents" / "skills"
    skills_root = Path(ak_path) / "skills"
    links = sync_skill_links(agents_skills, skills_root,
                             include=skill_scope.scope_for(cockpit, _kit_skill_ids(skills_root)))
    if links["removed"]:
        ui.info(f"Removed outdated kit skill links: {', '.join(links['removed'])}")
    if links["skipped"]:
        ui.warn(f"Skills not linked (name already used in {agents_skills}): "
                f"{', '.join(links['skipped'])}")


def scoped_skills_dir() -> Path:
    """The kit-managed directory of scoped skill links that OpenClaw reads through `skills.load.extraDirs`.
    Under the XDG state home (never /tmp, which is tmpfs and emptied on reboot)."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ai-resources" / "scoped-skills"


def sync_scoped_skills(ak_path: str, *, dry_run: bool = False) -> dict[str, list[str]]:
    """Make `scoped_skills_dir()` hold exactly one link per scoped kit skill (kit links only; a foreign entry is left)."""
    from ... import skill_scope
    skills_root = Path(ak_path) / "skills"
    return sync_skill_links(scoped_skills_dir(), skills_root, dry_run=dry_run,
                            include=skill_scope.scope_for("openclaw", _kit_skill_ids(skills_root)))


def multimodel_protocol_md(ak_path: str, gateway_url: str, mode: str = "multi-model",
                           route: str = "via_gateway") -> str:
    """Return the multi-model section for instruction files (empty in single-model mode).

    `route` is the cockpit's action from the compatibility matrix (`setup/compat.py`). Only a
    `via_gateway` cockpit (Claude Code, Aider) is told that its model calls go through the
    gateway: every other cockpit keeps its own model settings, and the text claims nothing else.

    The backend is read off the gateway URL rather than passed in: nine call sites
    across the cockpits already hand us the URL, and threading a parallel `backend`
    argument through all of them would let the two drift apart.
    """
    if mode == "single-model":
        return ""
    if route != "via_gateway":
        return (
            "## Multi-model routing\n\n"
            "This tool uses its own model settings; ai-resources does not choose its model. "
            "The role → model map is `~/.config/ai-resources/executors.yaml` "
            "(`ai-resources executors show` / `set`).\n"
        )
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
        "- Check the gateway with `ai-resources doctor`.\n"
        "- Prompt caching is lost on non-Anthropic routes, so a cheaper model can cost more per task. "
        "Keep review, security and verification on strong models.\n"
        f"{cost}"
    )


# The waiting rules every cockpit's kit block carries (ADR-0014 polling guard, ADR-0015 S5): one short section, so a
# cockpit without the Claude Code hook still gets the rule. Claude Code's CLAUDE.md block re-exports it.
WAITING_GUIDANCE = (
    "## Waiting\n\n"
    "- Never block one tool call on a long wait: run long work in the background, write to a file, and read it with short calls.\n"
    "- Wait with one blocking call (or a poll interval of 60-120 s or more); never loop `sleep`, `process poll` or "
    "`sessions_yield` at short intervals: every poll re-reads the whole context.\n"
    "- In a headless or subagent session never call the native AskUserQuestion (it hangs until the watchdog fires): "
    "return the question to the parent or use `ask_user`.\n"
)


def kit_instructions_md(tool: str, ak_path: str, gateway_url: str, mode: str, *,
                        native_skills: bool, extra: str = "", route: str = "instructions_only",
                        waiting: bool = True) -> str:
    """Kit block for the instruction files of cockpits other than Claude Code.

    `route` defaults to instructions-only: a cockpit gets the gateway sentence only when its caller
    passes `via_gateway` (Aider, in multi-model). `waiting` adds the short "Waiting" section; OpenClaw's own block
    carries its longer variant and passes False.
    """
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
        f"{WAITING_GUIDANCE + chr(10) if waiting else ''}"
        f"{multimodel_protocol_md(ak_path, gateway_url, mode, route)}"
    )


# Measured on openclaw 2026.9.6 (`openclaw worktrees create` against a throwaway repository, with
# `worktreeRoot` unset): `~/.openclaw/worktrees/<repoFingerprint>/<name>`, branch `openclaw/<name>`.
# The kit does not pin `worktreeRoot`: it documents the default.
OPENCLAW_WORKTREE_ROOT_NOTE = (
    "Managed worktrees live under `~/.openclaw/worktrees/<repo-fingerprint>/<name>` (measured on "
    "openclaw 2026.9.6, with `worktreeRoot` unset). Never create a worktree, a virtualenv or any "
    "scratch you need again under `/tmp`: it is tmpfs, held in RAM and emptied on every reboot, "
    "which is how the kit's test virtualenv and the old scratch worktrees died (T26)."
)


# The heading must stay byte-identical to the hand-written sections some workspaces already carry
# outside the markers: `verify` uses it to find those duplicates (T39).
OPENCLAW_LONG_RUNNING_HEADING = "## Long-running commands never block a tool call"
OPENCLAW_LONG_RUNNING_MD = (
    f"\n{OPENCLAW_LONG_RUNNING_HEADING}\n\n"
    "- A tool call that is still open holds a gateway stop until the stop timeout; then systemd "
    "SIGKILLs the gateway and every child it started (T39).\n"
    "- Work that may take more than about 2 minutes runs in the background, writes to a file, and "
    "is polled with short reads.\n"
    "- When `run_in_background` is not available (OpenClaw claude-cli children cannot background a "
    "Bash call), wrap the command in `timeout <N>` with N well under the tool-call limit, or detach "
    "it (`setsid nohup cmd >file 2>&1 &`) and poll the file with short reads.\n"
    "- Never put a long `sleep` or a wait loop inside one call.\n"
    "- Never call the native AskUserQuestion in a headless or subagent session: it hangs until the "
    "watchdog fires. Use `ask_user` with a `timeoutSeconds` of 120-300, then end the turn; never "
    "leave it open waiting. A subagent returns its question to the parent.\n"
    "- A cron job reports at each phase, so a timeout names the phase that hung.\n"
)


OPENCLAW_POLLING_HEADING = "## Waiting without polling"
OPENCLAW_POLLING_MD = (
    f"\n{OPENCLAW_POLLING_HEADING}\n\n"
    "- Every poll turn re-reads the whole context (poll turns were 21 % of the spend): wait with ONE blocking call "
    "per job, or poll no more often than every 60-120 s.\n"
    "- One wait per background job: `exec` or `agents_wait` with `awaitResults: true` blocks until it finishes; "
    "otherwise end the turn and let the completion event wake you.\n"
    "- No `process(poll)`, `sleep` or `sessions_yield` loops at short intervals; `process(poll)` with `timeout` is "
    "the one allowed wait, at most once per phase, then one short read of the output file.\n"
    "- Where hooks run, `kit_sleep_guard` refuses a `sleep` above 30 s, sleeps that add up above 30 s and any "
    "`while`/`until` wait loop.\n"
)


OPENCLAW_SCRATCH_HEADING = "## Scratch space"
OPENCLAW_SCRATCH_MD = (
    f"\n{OPENCLAW_SCRATCH_HEADING}\n\n"
    "- Never write directly under `~/.cache` or `/tmp`. Every clone, venv build, log, `mktemp` and pytest basetemp "
    "goes under `$AI_RESOURCES_SCRATCH` (`tmp/ pytest/ logs/ out/ clone/`), one directory per run: "
    "`~/.cache/ai-resources/runs/<role>-<slug>-<UTCstamp>-<pid>-<rand>/` (ADR-0020).\n"
    "- Dispatch exports it for you. Without it: `eval \"$(ai-resources scratch new --role <role> --slug <task> "
    "--owner-pid $$ --shell)\"`, and finish with `ai-resources scratch done \"$AI_RESOURCES_SCRATCH\" --status $?`. "
    "In a shell script, source `scripts/scratch.sh` and call `scratch_begin <role> <slug>` (it traps EXIT INT TERM).\n"
    "- Success removes the run. Failure keeps only `logs/`, for 3 days. A daily timer and every `scratch new` prune by "
    "`.scratch-meta.json` and owner-pid liveness, never by mtime.\n"
    "- Virtualenvs are shared, not per run: `ai-resources scratch venv --lock <requirements file>` (keyed by python "
    "version and lockfile hash, pruned after 30 idle days).\n"
)


def _squash(text: str) -> str:
    return " ".join(text.split())


def long_running_duplicate(text: str) -> "tuple[int, int, bool] | None":
    """The hand-written copy of the kit's long-running section OUTSIDE the managed block.

    (first line, end line exclusive, whether its body equals the kit's after whitespace normalisation), or None
    when the heading is not there. A section ends at the next level 1 or 2 heading, the managed block or EOF."""
    lines = text.splitlines(keepends=True)
    inside: set[int] = set()
    offset = 0
    rest = lines
    while rest:
        begin, end = _managed_block_span(rest)
        if begin is None:
            break
        stop = end if end is not None else len(rest) - 1
        inside.update(range(offset + begin, offset + stop + 1))
        offset += stop + 1
        rest = rest[stop + 1:]
    heading = re.compile(rf"^{re.escape(OPENCLAW_LONG_RUNNING_HEADING)}[ \t]*\r?$")
    start = next((i for i, ln in enumerate(lines) if i not in inside and heading.match(ln.rstrip("\n"))), None)
    if start is None:
        return None
    stop = len(lines)
    for j in range(start + 1, len(lines)):
        if j in inside or re.match(r"^#{1,2} ", lines[j]):
            stop = j
            break
    body = _squash("".join(lines[start + 1:stop]))
    kit = _squash(OPENCLAW_LONG_RUNNING_MD.split(OPENCLAW_LONG_RUNNING_HEADING, 1)[1])
    return start, stop, body == kit


def dedupe_long_running(path: Path, *, apply: bool, now: "str | None" = None) -> dict:
    """One AGENTS.md: {"status": "none"|"removed"|"would-remove"|"differs"|"symlink"|"write-failed", "diff": str,
    "backup": str}. A symlinked file is never rewritten; a failed write leaves the original and names the backup.

    An identical copy is removed (after a `<file>.bak-<UTC timestamp>` backup, only with `apply`); a copy that
    differs from the kit's text is never touched: the diff shows how. Every byte outside the section is kept."""
    import difflib
    import time as _time

    if path.is_symlink():
        # The rewrite replaces the file, which would silently replace the link by a regular file (and a write in
        # place would change whatever it points at). The operator names the real file instead (L3).
        return {"status": "symlink", "diff": "", "backup": "", "target": str(path.resolve())}
    raw = _read_raw(path)
    found = long_running_duplicate(raw)
    if found is None:
        return {"status": "none", "diff": "", "backup": ""}
    start, stop, same = found
    lines = raw.splitlines(keepends=True)
    if not same:
        kit = OPENCLAW_LONG_RUNNING_MD.strip("\n").splitlines(keepends=True)
        diff = "".join(difflib.unified_diff(kit, lines[start:stop], "kit text", f"{path} (hand-written)"))
        diff += "" if not diff or diff.endswith("\n") else "\n"     # the last line of a file may have none
        return {"status": "differs", "diff": diff, "backup": ""}
    kept = lines[:start] + lines[stop:]
    if start > 0 and kept[start - 1:start] and not kept[start - 1].strip() and (
            start >= len(kept) or not kept[start].strip()):
        kept.pop(start - 1)            # do not leave two blank lines where the section was
    new = "".join(kept)
    diff = "".join(difflib.unified_diff(lines, kept, str(path), f"{path} (deduplicated)"))
    diff += "" if not diff or diff.endswith("\n") else "\n"         # the last line of a file may have none
    if not apply:
        return {"status": "would-remove", "diff": diff, "backup": ""}
    stamp = now or _time.strftime("%Y%m%dT%H%M%SZ", _time.gmtime())
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    # O_EXCL|O_NOFOLLOW: a symlink (or anything else) planted at the backup name is never written through.
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(raw)
    # Write a temp file next to the original, then rename it over: a failed write leaves the original intact.
    tmp_name = ""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        tfd, tmp_name = _scratch.atomic_tmp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(tfd, "w", encoding="utf-8") as fh:
            fh.write(new)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except OSError as e:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return {"status": "write-failed", "diff": diff, "backup": str(backup), "error": str(e)}
    return {"status": "removed", "diff": diff, "backup": str(backup)}


def openclaw_agent_kit_md(ak_path: str, *, delegates: bool = True) -> str:
    """The kit block for an OpenClaw agent workspace `AGENTS.md`.

    Independent of the OpenClaw engine (the same text under `keep`, `direct`, `claude-code` and
    `antigravity`) and agent-agnostic: no agent id is substituted, so a workspace shared by two
    agents converges to the same bytes whichever one the loop visits last. `delegates` is a
    property of the WORKSPACE, not of an agent: it is False only when every agent that uses the
    workspace is the `claude` worker, which does not delegate to itself.
    """
    text = (
        "# ai-resources (OpenClaw agent)\n\n"
        f"Kit root: `{ak_path}`. After `brew upgrade ai-resources`, run `ai-resources setup`.\n\n"
        "## Kit workflow\n\n"
        "- Before any multi-step work, load the `kit-orchestration` skill: it explains how to run "
        "a workflow (steps, handoff, parallel groups, return format).\n"
        "- Workflows are the `workflow-*` skills (feature, bugfix, refactor, incident response, "
        "...). Pick one by reading each skill's description, which is its trigger. Definitions "
        f"live in `{ak_path}/workflows/`.\n"
        "- Work as a team with the kit roles rather than in one head: `planner`, "
        "`software-architect`, `implementer`, `tester`, `code-reviewer`, `security-auditor`, "
        "`verifier`, `doc-writer`. `/kit-plan <goal>` then `/kit-implement` drive that loop.\n"
        "- Never sign off your own work: only the `verifier` confirms acceptance criteria.\n"
        "- Size first: the orchestrator scores the task (`kit-orchestration`, \"Scorecard\") into LITE, STANDARD or FULL "
        "and prints it; no mode to pick (override: `--inline`, `--light`, `--full`); one concrete change: `/kit-plan --inline`.\n"
        "- Hand work between roles through the handoff file: `handoff.md` at the repo root (or "
        f"`.agent-output/handoff.md`); the shape is `{ak_path}/handoff.md.template`.\n"
    )
    text += OPENCLAW_LONG_RUNNING_MD
    text += OPENCLAW_POLLING_MD
    text += OPENCLAW_SCRATCH_MD
    text += (
        "\n## Worktrees\n\n"
        "- Do a task's work on its own branch in a managed worktree, not in the shared checkout: "
        "`openclaw worktrees create <repoRoot> --name <agent>-<task> [--base-ref <ref>]` prints the "
        "path and the branch (`openclaw/<name>`); work only there. The kit does not create, snapshot "
        "or clean worktrees: it delegates to `openclaw worktrees`.\n"
        "- `openclaw worktrees list` shows the active and the restorable ones; `remove <id>` snapshots "
        "then removes; `restore <id>` brings one back; `gc` reclaims the old ones.\n"
        f"- {OPENCLAW_WORKTREE_ROOT_NOTE}\n"
        "- The `using-git-worktrees` skill has the whole flow and the raw `git worktree` fallback for "
        "sessions outside OpenClaw.\n"
    )
    if delegates:
        text += (
            "\n## Delegation\n\n"
            "- Hand code and team work to the `claude` worker agent: "
            "`sessions_spawn agentId=claude cwd=<project> thread=true`, so the reply lands back "
            "in the same Telegram thread.\n"
            "- Do not do a project's coding in this workspace; delegate it, then report.\n"
        )
    return text


def kit_context_block(ak_path: str) -> str:
    """Return the standard kit-context footer used in subagent files."""
    return (
        f"\n\n---\n\n## Kit context\n\n"
        f"- **Kit root (`$AGENT_KIT`):** `{ak_path}` — skills, workflows, `handoff.md.template`.\n"
        f"- **In a workflow step:** read the skills named in your prompt, update the handoff file "
        f"(unless your step runs in a parallel group), and end with the return block from the "
        f"`kit-orchestration` skill.\n"
        f"- **Scratch:** use `$AI_RESOURCES_SCRATCH` (`tmp/ pytest/ logs/ out/ clone/`) for every clone, venv, log and "
        f"temp file. Never write directly under `~/.cache` or `/tmp` (ADR-0020).\n"
    )


def mcp_engram_block() -> dict:
    """Standard Engram MCP server block (matches Claude Desktop / Code / Gemini schema)."""
    return {
        "engram": {
            "command": "engram",
            "args": ["mcp"],
        }
    }
