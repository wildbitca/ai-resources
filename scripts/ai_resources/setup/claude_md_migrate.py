"""Guided migration of the operator's own blocks out of the global CLAUDE.md (ADR-0014 D3.1).

`~/.claude/CLAUDE.md` is loaded into every session of every project. The kit block in it is small and managed by
`write_managed_block`; the operator's own content is not the kit's to touch, except on an explicit confirmation, block by
block. This module offers the heaviest blocks (>= MIN_TOKENS, estimated at bytes/4) for a move to something Claude Code
loads on demand:

- a skill `~/.claude/skills/operator-<slug>/SKILL.md` whose description (typed by the operator) says when to load it, or
- a rule `~/.claude/rules/<slug>.md` with operator-supplied `paths:` globs,
- or keep (the default).

Rules of the move: an exclusive backup of CLAUDE.md before the first edit; the destination is written first, exclusively
(never over an existing file); only then is the block replaced by a one-line pointer; the text is moved verbatim (never
translated or reworded); nothing inside the kit block is ever read as a block or edited (its hash must be unchanged
afterwards); anything the parser cannot split with confidence (an unclosed code fence, no headings) is one block, never
split; never run headless.

Every move is recorded in `tracking.claude_md_migrations` (block id, sha256 of the destination as written, destination,
backup, pointer). `ai-resources setup --restore-claude-md` re-inlines a block only while its destination still hashes to
the recorded value and its pointer line is still in the file; otherwise it refuses and says why.
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import ui
from .cockpits import _shared

MIN_TOKENS = 300
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
POINTER_PREFIX = "Moved out of this file by ai-resources:"



def _read_raw(path: Path) -> str:
    """The text with line endings untouched (`Path.read_text(newline=)` needs Python 3.13)."""
    with path.open(encoding="utf-8", newline="") as fh:
        return fh.read()

def claude_md_path() -> Path:
    from .cockpits import claude
    return claude.CLAUDE_MD_PATH


def skills_dir() -> Path:
    from .cockpits import claude
    return claude.CONFIG_ROOT / "skills"


def rules_dir() -> Path:
    from .cockpits import claude
    return claude.CONFIG_ROOT / "rules"


def tokens_of(text: str) -> int:
    """Estimate: bytes / 4 (labelled an estimate wherever it is shown)."""
    return len(text.encode("utf-8")) // 4


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Block:
    id: str
    heading: str
    text: str                      # the block exactly as it sits in the file, trailing blank lines included
    core: str                      # the text without its trailing blank lines
    trailing: str                  # the trailing blank lines
    start: int                     # first line index in the file
    end: int                       # one past the last line index
    tokens: int


def _is_fence(line: str) -> bool:
    return line.lstrip().startswith(("```", "~~~"))


def _region_blocks(lines: list[str], lo: int, hi: int, level: int) -> list[Block]:
    """Blocks of lines[lo:hi]: one per heading of `level`. A region with an unclosed fence, or no heading of that
    level, is returned as ONE block (never split on a guess). Text before the first heading is not a block."""
    in_fence = False
    starts: list[int] = []
    for i in range(lo, hi):
        bare = lines[i].rstrip("\r\n")
        if _is_fence(bare):
            in_fence = not in_fence
            continue
        m = None if in_fence else _HEADING.match(bare)
        if m and len(m.group(1)) == level:
            starts.append(i)
    whole = in_fence or not starts
    if whole:
        if not "".join(lines[lo:hi]).strip():
            return []
        first = next((ln.strip() for ln in lines[lo:hi] if ln.strip()), "")
        return [_make_block(lines, lo, hi, _HEADING.sub(r"\2", first)[:80] or "(untitled)")]
    out = []
    for n, s in enumerate(starts):
        e = starts[n + 1] if n + 1 < len(starts) else hi
        m = _HEADING.match(lines[s].rstrip("\r\n"))
        out.append(_make_block(lines, s, e, m.group(2).strip() if m else "(untitled)"))
    return out


def _make_block(lines: list[str], s: int, e: int, heading: str) -> Block:
    text = "".join(lines[s:e])
    body = lines[s:e]
    cut = len(body)
    while cut > 1 and not body[cut - 1].strip():
        cut -= 1
    core = "".join(body[:cut])
    return Block(id=sha(text)[:8], heading=heading, text=text, core=core, trailing="".join(body[cut:]),
                 start=s, end=e, tokens=tokens_of(text))


def parse_blocks(raw: str) -> tuple[list[Block], str]:
    """(blocks outside the kit block, sha256 of the kit block text or "" when there is none)."""
    lines = raw.splitlines(keepends=True)
    begin, end = _shared._managed_block_span(lines)
    kit_hash = sha("".join(lines[begin:end + 1])) if begin is not None and end is not None else ""
    regions = [(0, len(lines))] if begin is None or end is None else [(0, begin), (end + 1, len(lines))]
    levels = []
    for lo, hi in regions:
        in_fence = False
        for i in range(lo, hi):
            bare = lines[i].rstrip("\r\n")
            if _is_fence(bare):
                in_fence = not in_fence
            elif not in_fence and (m := _HEADING.match(bare)):
                levels.append(len(m.group(1)))
    # The shallowest level with at least two headings: a lone `# Title` over `## sections` splits at the sections.
    level = next((lv for lv in sorted(set(levels)) if levels.count(lv) >= 2), min(levels) if levels else 1)
    blocks: list[Block] = []
    for lo, hi in regions:
        blocks += _region_blocks(lines, lo, hi, level)
    return blocks, kit_hash


def slugify(heading: str) -> str:
    ascii_ = unicodedata.normalize("NFKD", heading).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_).strip("-")[:40].strip("-")
    return slug or "block"


def pointer_for(kind: str, name: str, block_id: str) -> str:
    where = f"the skill `{name}`" if kind == "skill" else f"the rule `{name}`"
    return f"{POINTER_PREFIX} {where} (block {block_id}); `ai-resources setup --restore-claude-md` puts it back."


def _write_exclusive(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _free_destination(kind: str, slug: str) -> tuple[Path, str]:
    """(path, name) of a destination that does not exist yet: `-2`, `-3`... on a collision, never an overwrite."""
    for n in range(1, 100):
        suffix = "" if n == 1 else f"-{n}"
        if kind == "skill":
            name = f"operator-{slug}{suffix}"
            path = skills_dir() / name / "SKILL.md"
        else:
            name = f"{slug}{suffix}"
            path = rules_dir() / f"{name}.md"
        if not path.exists() and not path.parent.is_symlink():
            return path, name
    raise OSError("no free destination name")


def _yaml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def render_destination(kind: str, name: str, block: Block, *, description: str = "", paths: list[str] | None = None,
                       ends_with_newline: bool = True) -> tuple[str, int]:
    """(file text, offset where the verbatim block starts)."""
    if kind == "skill":
        head = f"---\nname: {name}\ndescription: {_yaml_str(description)}\n---\n\n"
    else:
        head = "---\npaths:\n" + "".join(f"  - {_yaml_str(p)}\n" for p in (paths or [])) + "---\n\n"
    body = block.core if ends_with_newline else block.core + "\n"
    return head + body, len(head)


def _replace_lines(raw: str, block: Block, replacement: str) -> str:
    lines = raw.splitlines(keepends=True)
    return "".join(lines[:block.start]) + replacement + "".join(lines[block.end:])


def migrate_block(raw: str, block: Block, kind: str, *, description: str = "", paths: list[str] | None = None,
                  backup: Path, now: datetime | None = None) -> tuple[str, dict]:
    """Write the destination for `block`, then return (the new CLAUDE.md text, the state record). The caller replaces
    the file. The destination is created exclusively; an existing file is never overwritten."""
    slug = slugify(block.heading)
    path, name = _free_destination(kind, slug)
    ends_nl = block.core.endswith(("\n", "\r"))
    text, offset = render_destination(kind, name, block, description=description, paths=paths, ends_with_newline=ends_nl)
    _write_exclusive(path, text)
    pointer = pointer_for(kind, name, block.id)
    nl = "\r\n" if block.core.endswith("\r\n") else "\n"
    replacement = pointer + (nl if ends_nl else "") + block.trailing
    record = {"block_id": block.id, "kind": kind, "sha256": sha(text), "destination": str(path), "body_offset": offset,
              "ends_with_newline": ends_nl, "backup": str(backup), "pointer": pointer,
              "timestamp": (now or datetime.now(timezone.utc)).isoformat()}
    return _replace_lines(raw, block, replacement), record


@dataclass(frozen=True)
class Decision:
    """One block the operator chose to move: nothing about it has been written yet (ADR-0016 D6)."""
    block_id: str
    heading: str
    tokens: int
    kind: str
    description: str = ""
    paths: tuple[str, ...] = ()


def plan(s, *, dry_run: bool = False) -> list[Decision]:
    """The asking half: one block at a time, heaviest first, default keep. Nothing is written. A dry run lists them and
    asks nothing."""
    path = claude_md_path()
    try:
        raw = _shared._read_raw(path)
    except OSError:
        return []
    if not raw:
        return []
    blocks, _kit = parse_blocks(raw)
    done = {r.get("block_id") for r in s.tracking.claude_md_migrations if isinstance(r, dict)}
    offers = sorted((b for b in blocks if b.tokens >= MIN_TOKENS and b.id not in done), key=lambda b: -b.tokens)
    total = tokens_of(raw)
    if not offers:
        ui.detail(f"Global CLAUDE.md: ~{total:,} tokens (estimate); no block of your own is heavy enough to move.")
        return []
    ui.section(5, 5, "Global CLAUDE.md - move heavy blocks out")
    ui.info(f"{path} is loaded into every session: ~{total:,} tokens (estimate). These blocks of your own content are "
            f"{MIN_TOKENS}+ tokens each; moving one makes it load only when it is relevant. Nothing moves unless you say so, "
            "and nothing moves until Apply.")
    if dry_run:
        for b in offers:
            ui.detail(f"DRY RUN: would offer '{b.heading}' (~{b.tokens:,} tokens).")
        return []
    out: list[Decision] = []
    for offer in offers:
        choice = ui.select(
            f"'{offer.heading}' (~{offer.tokens:,} tokens, estimate): what should happen to this block?",
            [ui.Choice("Keep it in CLAUDE.md", value="keep"),
             ui.Choice("Move it to a skill (loaded when its description matches the task)", value="skill"),
             ui.Choice("Move it to a rule (loaded when files matching your paths are touched)", value="rule")],
            default="keep")
        if choice not in ("skill", "rule"):
            continue
        description, paths = "", None
        if choice == "skill":
            proposal = f"Operator rules: {offer.heading}. Load when the task involves this topic."
            description = (ui.text("When should Claude load this skill? (the description it reads)", default=proposal) or "").strip()
            if not description:
                continue
        else:
            raw_paths = ui.text("Which files trigger this rule? (comma-separated globs, e.g. src/**/*.ts)", default="") or ""
            paths = [p.strip() for p in raw_paths.split(",") if p.strip()]
            if not paths:
                ui.info("A rule needs at least one path glob; the block stays in CLAUDE.md.")
                continue
        out.append(Decision(offer.id, offer.heading, offer.tokens, choice, description, tuple(paths or ())))
    return out


def apply(s, decisions: list[Decision], *, now: datetime | None = None) -> int:
    """The writing half, at Apply: move each chosen block (backup first, destination created exclusively, the kit block
    never touched). Returns how many moved. Same bytes as the old immediate step."""
    path = claude_md_path()
    backup: Path | None = None
    moved = 0
    for d in decisions:
        current = _shared._read_raw(path)
        fresh, kit_before = parse_blocks(current)
        target = next((b for b in fresh if b.id == d.block_id), None)
        if target is None:
            ui.warn(f"'{d.heading}' changed while asking; left in place.")
            continue
        try:
            if backup is None:
                stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
                backup = _shared._create_backup(path, current, stamp)
            new_text, record = migrate_block(current, target, d.kind, description=d.description,
                                             paths=list(d.paths) or None, backup=backup, now=now)
            _fresh, kit_after = parse_blocks(new_text)
            if kit_after != kit_before:
                raise OSError("the kit block would change")             # never edit inside the kit block
            write_to = _shared._write_target(path)
            if write_to is None:
                continue
            _shared._replace_atomically(write_to, new_text)
        except OSError as e:
            ui.warn(f"Could not move '{d.heading}': {e}. CLAUDE.md is unchanged for this block.")
            continue
        s.tracking.claude_md_migrations.append(record)
        ui.ok(f"Moved '{d.heading}' to {record['destination']} (backup: {backup})")
        ui.record_action(f"moved CLAUDE.md block {d.block_id}")
        moved += 1
    return moved


def run_step(s, *, dry_run: bool = False, now: datetime | None = None) -> int:
    """`plan` then `apply` in one go, for a caller that has no Apply of its own. The wizard queues `apply` instead."""
    apply(s, plan(s, dry_run=dry_run), now=now)
    return 0


def restore_one(record: dict, *, path: Path | None = None) -> tuple[bool, str]:
    """Re-inline one moved block. (False, reason) when the destination was edited or the pointer is gone."""
    path = path or claude_md_path()
    dest = Path(str(record.get("destination") or ""))
    try:
        text = _read_raw(dest)
    except OSError:
        return False, f"{dest} is missing"
    if sha(text) != record.get("sha256"):
        return False, f"{dest} was edited since the move; merge it back by hand (the original is in {record.get('backup')})"
    body = text[int(record.get("body_offset") or 0):]
    ends_nl = bool(record.get("ends_with_newline", True))
    core = body if ends_nl else body[:-1]
    raw = _shared._read_raw(path)
    lines = raw.splitlines(keepends=True)
    begin, end = _shared._managed_block_span(lines)
    pointer = str(record.get("pointer") or "")
    hits = [i for i, ln in enumerate(lines)
            if ln.rstrip("\r\n") == pointer and not (begin is not None and end is not None and begin <= i <= end)]
    if len(hits) != 1:
        return False, "the pointer line is not in CLAUDE.md exactly once (it was edited or removed)"
    i = hits[0]
    new = "".join(lines[:i]) + core + "".join(lines[i + 1:])
    target = _shared._write_target(path)
    if target is None:
        return False, f"{path} cannot be written"
    _shared._replace_atomically(target, new)
    try:
        dest.unlink()
        if record.get("kind") == "skill":
            dest.parent.rmdir()
    except OSError:
        pass
    return True, ""


def cmd_restore(*, say: Callable[[str], None] = print) -> int:
    """`ai-resources setup --restore-claude-md`: re-inline every moved block that can be restored safely."""
    from . import state
    records = list(state.load().tracking.claude_md_migrations)
    if not records:
        say("Nothing to restore: the kit has not moved any block out of CLAUDE.md.")
        return 0
    kept: list[dict] = []
    for rec in reversed(records):
        ok, why = restore_one(rec)
        if ok:
            say(f"Restored block {rec.get('block_id')} into {claude_md_path()}")
        else:
            say(f"Not restored (block {rec.get('block_id')}): {why}")
            kept.append(rec)
    keep_ids = {json_key(r) for r in kept}

    def mutate(fresh) -> None:
        fresh.tracking.claude_md_migrations = [r for r in fresh.tracking.claude_md_migrations if json_key(r) in keep_ids]

    if not state.update(mutate):
        say("The setup state is busy: the blocks were restored but their records were not updated. Run it again.")
        return 1
    return 1 if kept else 0


def json_key(record: dict) -> str:
    import json
    return json.dumps(record, sort_keys=True)


def skip_reason(s) -> str | None:
    from . import hygiene
    if hygiene.is_headless():
        return "headless"
    selected = (s.profile.customizations.get("__targets__") or {}).get("selected") or []
    if "claude" not in selected and not (s.cockpits.get("claude") and s.cockpits["claude"].installed):
        return "claude not selected"
    return None
