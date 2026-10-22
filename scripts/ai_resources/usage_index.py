"""Usage index: which MCP servers, skills, plugins and subagents the operator actually called (ADR-0014 S5).

The hygiene step (offering to disable what is never used) and the audit need one answer to "was X called in the last
N days?". Reading 3 GB of transcripts each time is not acceptable, so the answer is an index under
`~/.cache/ai-resources/usage-index.json`: for every transcript, per day, how many times each server, skill, plugin and
subagent was called. A build re-reads only a transcript whose (mtime, size) changed; a deleted transcript leaves the
index; a corrupt or foreign-version cache is rebuilt. Read-only on the transcripts. The first heavy build prints a
progress message and nothing here is ever run silently in the setup hot path.

Names follow Claude Code:
- an MCP tool is `mcp__<server>__<tool>`; a plugin's server is `mcp__plugin_<plugin>_<server>__<tool>` (the server key
  is kept as written and the plugin is derived from the `plugin_` prefix);
- the `Skill` tool names `<skill>` or `<plugin>:<skill>`; the `Task`/`Agent` tool names a `subagent_type`
  (`<plugin>:<agent>` for a plugin's agent).
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

VERSION = 1
HEAVY_FILES = 200            # a first build reading at least this many transcripts announces itself
AGENT_TOOLS = ("Task", "Agent")


def cache_path() -> Path:
    return Path.home() / ".cache" / "ai-resources" / "usage-index.json"


def default_roots() -> list[Path]:
    root = Path.home() / ".claude" / "projects"
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


class Counts(dict):
    """{name: calls}; a name that was never called reads as 0."""

    def __missing__(self, key):
        return 0


def short(name: str) -> str:
    """`mcp__grafana__query` -> `grafana`; a non-MCP tool name is returned as it is (cost-analysis report.py `short`)."""
    return name.split("__")[1] if name.startswith("mcp__") and name.count("__") >= 2 else name


def plugin_of_server(server: str) -> str:
    """`plugin_engram_engram` -> `engram`: the plugin is the part between `plugin_` and the last underscore."""
    if not server.startswith("plugin_"):
        return ""
    rest = server[len("plugin_"):]
    return rest.rsplit("_", 1)[0] if "_" in rest else rest


def _scan_file(path: Path, fallback_day: str) -> dict:
    """{"mcp": {server: {day: n}}, "skill": {...}, "agent": {...}} for one transcript."""
    out: dict = {"mcp": {}, "skill": {}, "agent": {}}
    seen: set[str] = set()

    def bump(kind: str, name: str, day: str) -> None:
        d = out[kind].setdefault(name, {})
        d[day] = d.get(day, 0) + 1

    try:
        fh = open(path, encoding="utf-8", errors="ignore")
    except OSError:
        return out
    with fh:
        for line in fh:
            if '"tool_use"' not in line:
                continue
            try:
                d = json.loads(line)
                msg = d.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
            except (ValueError, AttributeError):
                continue
            if not isinstance(content, list):
                continue
            ts = d.get("timestamp")
            day = str(ts)[:10] if isinstance(ts, str) and len(ts) >= 10 else fallback_day
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                key = block.get("id")
                if key:
                    if key in seen:
                        continue                       # a streamed message is written several times
                    seen.add(key)
                name, inp = str(block.get("name") or ""), block.get("input")
                inp = inp if isinstance(inp, dict) else {}
                if name.startswith("mcp__"):
                    bump("mcp", short(name), day)
                elif name == "Skill" and isinstance(inp.get("skill"), str):
                    bump("skill", inp["skill"], day)
                elif name in AGENT_TOOLS and isinstance(inp.get("subagent_type"), str):
                    bump("agent", inp["subagent_type"], day)
    return out


class Index:
    def __init__(self, files: dict | None = None, stats: dict | None = None):
        self.files: dict[str, dict] = files or {}
        self.stats: dict[str, int] = stats or {"read": 0, "cached": 0, "dropped": 0}

    def _window(self, kind: str, window_days: int, now: datetime | None) -> Counts:
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=window_days)).strftime("%Y-%m-%d")
        out = Counts()
        for entry in self.files.values():
            for name, days in (entry.get(kind) or {}).items():
                n = sum(c for day, c in days.items() if day >= cutoff)
                if n:
                    out[name] = out.get(name, 0) + n
        return out

    def by_server(self, window_days: int = 30, now: datetime | None = None) -> Counts:
        return self._window("mcp", window_days, now)

    def by_skill(self, window_days: int = 30, now: datetime | None = None) -> Counts:
        return self._window("skill", window_days, now)

    def by_agent(self, window_days: int = 30, now: datetime | None = None) -> Counts:
        return self._window("agent", window_days, now)

    def by_plugin(self, window_days: int = 30, now: datetime | None = None) -> Counts:
        """Calls into a plugin: its MCP servers, its namespaced skills and its namespaced agents."""
        out = Counts()
        for server, n in self.by_server(window_days, now).items():
            if plugin_of_server(server):
                out[plugin_of_server(server)] = out.get(plugin_of_server(server), 0) + n
        for counts in (self.by_skill(window_days, now), self.by_agent(window_days, now)):
            for name, n in counts.items():
                if ":" in name:
                    out[name.split(":", 1)[0]] = out.get(name.split(":", 1)[0], 0) + n
        return out

    def unused(self, kind: str, name: str, window_days: int = 30, now: datetime | None = None) -> bool:
        """True when `name` (kind: server | skill | agent | plugin) has no call in the window."""
        table = {"server": self.by_server, "skill": self.by_skill, "agent": self.by_agent,
                 "plugin": self.by_plugin}[kind](window_days, now)
        return table[name] == 0


def _load_cache(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict) or doc.get("version") != VERSION or not isinstance(doc.get("files"), dict):
        return {}
    return doc["files"]


def _save_cache(path: Path, files: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"version": VERSION, "files": files}, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass                                           # a cache that cannot be written is rebuilt next time


def _transcripts(roots) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        root = Path(root)
        if root.is_dir():
            found += [p for p in root.rglob("*.jsonl") if p.is_file()]
    return sorted(set(found))


def build(roots=None, cache_path: Path | None = None, say: Callable[[str], None] = print) -> Index:
    """Refresh the index from the transcripts under `roots` (default: every Claude Code project) and return it."""
    path = Path(cache_path) if cache_path else globals()["cache_path"]()
    roots = default_roots() if roots is None else roots
    cached = _load_cache(path)
    files: dict[str, dict] = {}
    todo: list[tuple[Path, int, int]] = []
    stats = {"read": 0, "cached": 0, "dropped": 0}
    present = _transcripts(roots)
    for f in present:
        try:
            st = f.stat()
        except OSError:
            continue
        old = cached.get(str(f))
        if old and old.get("m") == st.st_mtime_ns and old.get("s") == st.st_size:
            files[str(f)] = old
            stats["cached"] += 1
        else:
            todo.append((f, st.st_mtime_ns, st.st_size))
    stats["dropped"] = len(set(cached) - {str(f) for f in present}) if cached else 0
    if todo and not cached and len(todo) >= HEAVY_FILES:
        say(f"Usage index: first build, reading {len(todo)} transcripts once (cached under {path.parent}; "
            "later runs read only what changed).")
    for f, m, s in todo:
        fallback = datetime.fromtimestamp(m / 1e9, timezone.utc).strftime("%Y-%m-%d")
        entry = _scan_file(f, fallback)
        entry["m"], entry["s"] = m, s
        files[str(f)] = entry
        stats["read"] += 1
    if todo or stats["dropped"]:
        _save_cache(path, files)
    return Index(files, stats)


def usage_by_server(window_days: int = 30, now: datetime | None = None, say: Callable[[str], None] = print,
                    roots=None) -> Counts:
    """The single interface (plan S5): MCP server -> calls in the last `window_days`; a never-called server reads 0."""
    return build(roots=roots, say=say).by_server(window_days, now)
